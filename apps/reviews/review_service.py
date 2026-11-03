import logging
from decimal import Decimal, ROUND_HALF_UP

import cloudinary.uploader
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers

from apps.authentication.models import WorkerProfile
from apps.bookings.models import BookingSchedule
from apps.common.cloudinary_storage import ensure_cloudinary_configured, upload_image
from apps.services.utils import extract_cloudinary_public_id
from apps.worker.models import BookingAssignment

from .models import Review, ReviewImage


logger = logging.getLogger(__name__)


def review_queryset():
    return Review.objects.select_related(
        'assignment',
        'assignment__worker',
        'assignment__schedule',
        'assignment__schedule__booking',
        'assignment__schedule__booking__customer',
        'assignment__schedule__booking__service',
        'replied_by',
    ).prefetch_related('images')


def eligible_assignments_for_customer(customer):
    return BookingAssignment.objects.filter(
        schedule__booking__customer=customer,
        schedule__status=BookingSchedule.Status.COMPLETED,
        status=BookingAssignment.Status.ACCEPTED,
        review__isnull=True,
    ).select_related(
        'worker', 'schedule', 'schedule__booking', 'schedule__booking__service',
    ).order_by('-schedule__actual_end', '-schedule__scheduled_end')


def _refresh_worker_average_rating(worker_id):
    result = Review.objects.filter(
        assignment__worker_id=worker_id,
        is_visible=True,
    ).aggregate(average=Avg('rating'))
    average = result['average']
    normalized = (
        Decimal(str(average)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if average is not None else Decimal('0.00')
    )
    WorkerProfile.objects.filter(user_id=worker_id).update(average_rating=normalized)
    return normalized


def _validate_assignment_for_review(assignment, customer):
    if assignment.schedule.booking.customer_id != customer.id:
        raise serializers.ValidationError({'assignment_id': 'Buổi làm việc không thuộc đơn dịch vụ của bạn.'})
    if assignment.status != BookingAssignment.Status.ACCEPTED:
        raise serializers.ValidationError({'assignment_id': 'Nhân viên không còn được phân công cho buổi này.'})
    if assignment.schedule.status != BookingSchedule.Status.COMPLETED:
        raise serializers.ValidationError({'assignment_id': 'Chỉ có thể đánh giá sau khi buổi làm việc hoàn thành.'})
    if Review.objects.filter(assignment=assignment).exists():
        raise serializers.ValidationError({'assignment_id': 'Buổi làm việc này đã được đánh giá.'})


def _save_review_images(review, image_files, uploaded_public_ids):
    folder = f'{settings.CLOUDINARY_REVIEW_IMAGE_FOLDER}/review_{review.id}'
    for image_file in image_files:
        uploaded = upload_image(
            image_file,
            folder=folder,
            public_id_prefix='review_image',
            field_name='images',
        )
        uploaded_public_ids.append(uploaded['public_id'])
        ReviewImage.objects.create(review=review, image=uploaded['url'])


def _cleanup_cloudinary_images(public_ids):
    for public_id in public_ids:
        try:
            cloudinary.uploader.destroy(public_id, resource_type='image')
        except Exception:
            logger.exception('Không thể cleanup ảnh Cloudinary %s.', public_id)


def _delete_cloudinary_images(image_urls):
    for image_url in image_urls:
        public_id = extract_cloudinary_public_id(image_url)
        if not public_id:
            continue
        try:
            result = cloudinary.uploader.destroy(public_id, resource_type='image')
            if result.get('result') not in ('ok', 'not found'):
                logger.error('Cloudinary không xoá được ảnh %s: %s', public_id, result)
        except Exception:
            logger.exception('Không thể xoá ảnh Cloudinary %s.', public_id)


def create_review(*, customer, assignment_id, rating, comment=None, images=None):
    image_files = images or []
    uploaded_public_ids = []
    try:
        with transaction.atomic():
            assignment = get_object_or_404(
                BookingAssignment.objects.select_for_update().select_related(
                    'worker', 'schedule', 'schedule__booking', 'schedule__booking__customer',
                ),
                pk=assignment_id,
            )
            _validate_assignment_for_review(assignment, customer)
            review = Review.objects.create(
                assignment=assignment,
                rating=rating,
                comment=(comment or '').strip() or None,
            )
            _save_review_images(review, image_files, uploaded_public_ids)
            _refresh_worker_average_rating(assignment.worker_id)
            return review
    except IntegrityError as exc:
        _cleanup_cloudinary_images(uploaded_public_ids)
        raise serializers.ValidationError(
            {'assignment_id': 'Buổi làm việc này đã được đánh giá.'}
        ) from exc
    except serializers.ValidationError:
        _cleanup_cloudinary_images(uploaded_public_ids)
        raise
    except Exception as exc:
        _cleanup_cloudinary_images(uploaded_public_ids)
        logger.exception('Upload ảnh review thất bại.')
        raise serializers.ValidationError(
            {'images': 'Không thể upload ảnh đánh giá. Vui lòng thử lại.'}
        ) from exc


def update_customer_review(
    *, review, rating=None, comment=serializers.empty, images=None,
    delete_image_ids=None,
):
    image_files = images or []
    image_ids_to_delete = set(delete_image_ids or [])
    uploaded_public_ids = []
    deleted_image_urls = []

    try:
        with transaction.atomic():
            locked_review = Review.objects.select_for_update().select_related('assignment').get(pk=review.pk)
            if not locked_review.can_edit:
                raise serializers.ValidationError({
                    'review': 'Đã hết thời hạn chỉnh sửa. Bạn chỉ được sửa đánh giá trong 30 ngày kể từ lần gửi đầu tiên.'
                })
            if image_ids_to_delete:
                ensure_cloudinary_configured(field_name='delete_image_ids')
            existing_images = ReviewImage.objects.select_for_update().filter(review=locked_review)
            owned_delete_ids = set(
                existing_images.filter(id__in=image_ids_to_delete).values_list('id', flat=True)
            )
            invalid_ids = image_ids_to_delete - owned_delete_ids
            if invalid_ids:
                raise serializers.ValidationError({
                    'delete_image_ids': 'Một hoặc nhiều ảnh không thuộc đánh giá này.'
                })

            remaining_count = existing_images.count() - len(owned_delete_ids) + len(image_files)
            if remaining_count > settings.REVIEW_MAX_IMAGES:
                raise serializers.ValidationError({
                    'images': f'Mỗi đánh giá chỉ được có tối đa {settings.REVIEW_MAX_IMAGES} ảnh.'
                })

            locked_review.edited_at = timezone.now()
            update_fields = ['updated_at', 'edited_at']
            if rating is not None:
                locked_review.rating = rating
                update_fields.append('rating')
            if comment is not serializers.empty:
                locked_review.comment = (comment or '').strip() or None
                update_fields.append('comment')
            locked_review.save(update_fields=update_fields)

            _save_review_images(locked_review, image_files, uploaded_public_ids)
            images_to_delete = list(existing_images.filter(id__in=owned_delete_ids))
            deleted_image_urls = [image.image for image in images_to_delete]
            if owned_delete_ids:
                existing_images.filter(id__in=owned_delete_ids).delete()

            _refresh_worker_average_rating(locked_review.assignment.worker_id)
    except serializers.ValidationError:
        _cleanup_cloudinary_images(uploaded_public_ids)
        raise
    except Exception as exc:
        _cleanup_cloudinary_images(uploaded_public_ids)
        logger.exception('Cập nhật ảnh review thất bại.')
        raise serializers.ValidationError(
            {'images': 'Không thể cập nhật ảnh đánh giá. Vui lòng thử lại.'}
        ) from exc

    _delete_cloudinary_images(deleted_image_urls)
    return locked_review


@transaction.atomic
def set_review_visibility(*, review, is_visible):
    locked_review = Review.objects.select_for_update().select_related('assignment').get(pk=review.pk)
    locked_review.is_visible = is_visible
    locked_review.save(update_fields=['is_visible', 'updated_at'])
    _refresh_worker_average_rating(locked_review.assignment.worker_id)
    return locked_review


@transaction.atomic
def reply_to_review(*, review, admin, reply):
    locked_review = Review.objects.select_for_update().get(pk=review.pk)
    locked_review.admin_reply = reply.strip()
    locked_review.replied_by = admin
    locked_review.replied_at = timezone.now()
    locked_review.save(update_fields=['admin_reply', 'replied_by', 'replied_at', 'updated_at'])
    return locked_review


def worker_review_summary(worker):
    queryset = Review.objects.filter(assignment__worker=worker, is_visible=True)
    aggregate = queryset.aggregate(average=Avg('rating'), total=Count('id'))
    distribution = {str(rating): 0 for rating in range(1, 6)}
    for row in queryset.values('rating').annotate(total=Count('id')):
        distribution[str(row['rating'])] = row['total']
    average = aggregate['average']
    return {
        'average_rating': (
            str(Decimal(str(average)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            if average is not None else '0.00'
        ),
        'total_reviews': aggregate['total'],
        'rating_distribution': distribution,
    }
