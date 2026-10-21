from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from apps.payments.models import Payment
from apps.bookings.activity_service import record_booking_activity
from apps.bookings.models import Booking, BookingActivity, BookingSchedule, BookingScheduleImage
from apps.common.cloudinary_storage import (
    upload_image,
    delete_uploaded_file,
)
from apps.notifications.services import (
    notify_customer_schedule_completed,
    notify_customer_worker_checked_in,
)
from apps.vouchers.voucher_service import mark_user_voucher_used

from .models import BookingAssignment
from .assignment_service import OPEN_BOOKING_STATUSES
from apps.wallets import earning_service
import math

CHECKIN_EARLY_MINUTES = getattr(settings, 'CHECKIN_EARLY_MINUTES', 30)
CHECKIN_LATE_MINUTES = getattr(settings, 'CHECKIN_LATE_MINUTES', 30)
CHECKOUT_GRACE_MINUTES = getattr(settings, 'CHECKOUT_GRACE_MINUTES', 30)

CHECKIN_MAX_DISTANCE_METERS = getattr(settings, 'CHECKIN_MAX_DISTANCE_METERS', 300)
# GPS sai số quá lớn thì không đủ tin cậy để so khoảng cách.
CHECKIN_MAX_ACCURACY_METERS = getattr(settings, 'CHECKIN_MAX_ACCURACY_METERS', 100)
# Địa chỉ cũ nhập tay không có toạ độ: True = bỏ qua kiểm tra khoảng cách,
# False = chặn check-in (nên đặt False khi dữ liệu cũ đã được dọn).
CHECKIN_ALLOW_MISSING_ADDRESS_COORDS = getattr(settings, 'CHECKIN_ALLOW_MISSING_ADDRESS_COORDS', True)

MAX_IMAGES_PER_TYPE = getattr(settings, 'MAX_SCHEDULE_IMAGES_PER_TYPE', 5)

def _get_my_accepted_schedule(*, schedule_id, worker, for_update=True):
    if not BookingAssignment.objects.filter(
        schedule_id=schedule_id, worker=worker,
        status=BookingAssignment.Status.ACCEPTED,
    ).exists():
        raise serializers.ValidationError({'schedule': 'Bạn chưa nhận buổi làm việc này.'})

    queryset = BookingSchedule.objects.select_related('booking')
    if for_update:
        booking_id = get_object_or_404(
            BookingSchedule.objects.only('booking_id'), pk=schedule_id,
        ).booking_id
        Booking.objects.select_for_update(of=('self',)).get(pk=booking_id)  # lock booking trước
        queryset = queryset.select_for_update(of=('self',))

    return get_object_or_404(queryset, pk=schedule_id)

def _distance_meters(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _verify_worker_near_address(*, booking, latitude, longitude, accuracy):
    """Trả về khoảng cách (m) hoặc None nếu địa chỉ không có toạ độ."""
    if accuracy is not None and accuracy > CHECKIN_MAX_ACCURACY_METERS:
        raise serializers.ValidationError({
            'location': f'Tín hiệu GPS chưa đủ chính xác (sai số ~{round(accuracy)} m). '
                        f'Hãy ra nơi thoáng hơn, bật GPS độ chính xác cao rồi thử lại.'
        })

    address = getattr(booking, 'address', None)
    addr_lat = getattr(address, 'latitude', None)
    addr_lng = getattr(address, 'longitude', None)
    if addr_lat is None or addr_lng is None:
        if CHECKIN_ALLOW_MISSING_ADDRESS_COORDS:
            return None
        raise serializers.ValidationError({
            'location': 'Địa chỉ của khách chưa có toạ độ nên không thể xác thực vị trí. '
                        'Vui lòng liên hệ CleanWise.'
        })

    distance = _distance_meters(latitude, longitude, float(addr_lat), float(addr_lng))
    if distance > CHECKIN_MAX_DISTANCE_METERS:
        raise serializers.ValidationError({
            'location': f'Bạn đang cách địa chỉ của khách khoảng {round(distance)} m. '
                        f'Cần ở trong bán kính {CHECKIN_MAX_DISTANCE_METERS} m để check-in.'
        })
    return distance


@transaction.atomic
def check_in(*, schedule_id, worker, latitude, longitude, accuracy=None):
    schedule = _get_my_accepted_schedule(schedule_id=schedule_id, worker=worker)
    booking = Booking.objects.select_related('address').get(pk=schedule.booking_id)

    if schedule.status != BookingSchedule.Status.PENDING:
        raise serializers.ValidationError({'schedule': 'Buổi làm việc không ở trạng thái chờ thực hiện.'})

    if booking.status not in OPEN_BOOKING_STATUSES:
        raise serializers.ValidationError({'schedule': 'Đơn hàng không ở trạng thái có thể bắt đầu.'})

    now = timezone.now()
    earliest = schedule.scheduled_start - timedelta(minutes=CHECKIN_EARLY_MINUTES)
    latest = schedule.scheduled_start + timedelta(minutes=CHECKIN_LATE_MINUTES)

    if now < earliest:
        raise serializers.ValidationError({
            'schedule': f'Chưa đến giờ check-in. Bạn chỉ có thể check-in từ '
                        f'{CHECKIN_EARLY_MINUTES} phút trước giờ hẹn.'
        })
    if now > latest:
        raise serializers.ValidationError({
            'schedule': f'Đã quá {CHECKIN_LATE_MINUTES} phút so với giờ hẹn, '
                        f'không thể check-in. Vui lòng liên hệ khách hàng/CleanWise.'
        })

    distance = _verify_worker_near_address(
        booking=booking, latitude=latitude, longitude=longitude, accuracy=accuracy,
    )

    schedule.actual_start = now
    schedule.status = BookingSchedule.Status.IN_PROGRESS
    schedule.save(update_fields=['actual_start', 'status', 'updated_at'])

    if booking.status in (Booking.Status.PENDING, Booking.Status.ASSIGNED):
        booking.status = Booking.Status.IN_PROGRESS
        booking.save(update_fields=['status', 'updated_at'])

    if booking.user_voucher_id:
        mark_user_voucher_used(user_voucher_id=booking.user_voucher_id)

    notify_customer_worker_checked_in(schedule, worker)
    record_booking_activity(
        booking=booking,
        schedule=schedule,
        actor=worker,
        event_type=BookingActivity.EventType.CHECKED_IN,
        message=f'Nhân viên check-in buổi {schedule.sequence_no}.',
        new_data={
            'actual_start': now.isoformat(),
            'latitude': latitude,
            'longitude': longitude,
            'accuracy': accuracy,
            'distance_m': round(distance) if distance is not None else None,
        },
    )
    return schedule


@transaction.atomic
def check_out(*, schedule_id, worker, completion_note=None):
    schedule = _get_my_accepted_schedule(schedule_id=schedule_id, worker=worker)
    booking = Booking.objects.select_for_update(of=('self',)).get(pk=schedule.booking_id)

    if schedule.status != BookingSchedule.Status.IN_PROGRESS:
        raise serializers.ValidationError({'schedule': 'Buổi làm việc chưa được Check-in hoặc đã hoàn thành.'})

    now = timezone.now()
    checkout_deadline = schedule.scheduled_end + timedelta(minutes=CHECKOUT_GRACE_MINUTES)
    if now > checkout_deadline:
        raise serializers.ValidationError({
            'schedule': f'Đã quá {CHECKOUT_GRACE_MINUTES} phút so với giờ kết thúc dự kiến, '
                        f'không thể tự check-out. Hệ thống sẽ tự động hoàn thành buổi làm này.'
        })

    
    before_count = schedule.images.filter(image_type=BookingScheduleImage.ImageType.BEFORE).count()
    after_count = schedule.images.filter(image_type=BookingScheduleImage.ImageType.AFTER).count()
    if before_count == 0:
        raise serializers.ValidationError({'images': 'Cần ít nhất 1 ảnh trước khi làm.'})
    if after_count == 0:
        raise serializers.ValidationError({'images': 'Cần ít nhất 1 ảnh sau khi làm.'})

    schedule.actual_end = now
    schedule.status = BookingSchedule.Status.COMPLETED
    schedule.completion_note = completion_note  # THÊM
    schedule.save(update_fields=['actual_end', 'status', 'completion_note', 'updated_at'])  # SỬA thêm field

    still_active = booking.schedules.filter(
        status__in=(BookingSchedule.Status.PENDING, BookingSchedule.Status.IN_PROGRESS),
    ).exclude(pk=schedule.pk).exists()

    if not still_active and booking.status == Booking.Status.IN_PROGRESS:
        booking.status = Booking.Status.COMPLETED
        booking.save(update_fields=['status', 'updated_at'])

    earning_service.record_schedule_earning(schedule=schedule, booking=booking, worker=worker)
    if booking.status != Booking.Status.COMPLETED:
        notify_customer_schedule_completed(schedule)
    record_booking_activity(
        booking=booking,
        schedule=schedule,
        actor=worker,
        event_type=BookingActivity.EventType.CHECKED_OUT,
        message=f'Nhân viên check-out buổi {schedule.sequence_no}.',
        new_data={
            'actual_end': now.isoformat(),
            'completion_note': completion_note,
        },
    )
    return schedule


def upload_schedule_image(*, schedule_id, worker, file, image_type, note=None):
    schedule = _get_my_accepted_schedule(
        schedule_id=schedule_id,
        worker=worker,
        for_update=False,
    )

    if schedule.status != BookingSchedule.Status.IN_PROGRESS:
        raise serializers.ValidationError({
            'schedule': 'Chỉ được thêm ảnh khi buổi làm việc đang thực hiện.'
        })

    current_count = BookingScheduleImage.objects.filter(
        schedule=schedule,
        image_type=image_type,
    ).count()

    if current_count >= MAX_IMAGES_PER_TYPE:
        raise serializers.ValidationError({
            'image': f'Mỗi loại ảnh tối đa {MAX_IMAGES_PER_TYPE} ảnh.'
        })

    # 1. Upload ảnh lên Cloudinary
    uploaded = upload_image(
        file,
        folder=f'schedules/{schedule.id}',
        public_id_prefix=image_type.lower(),
        field_name='image',
    )

    try:
        # 2. Tạo record trong DB
        last_order = BookingScheduleImage.objects.filter(
            schedule=schedule
        ).count()

        return BookingScheduleImage.objects.create(
            uploaded_by=worker,
            schedule=schedule,
            image_type=image_type,
            image=uploaded['url'],
            note=note or None,
            sort_order=last_order,
        )

    except Exception:
        # 3. DB lỗi → xóa ảnh vừa upload trên Cloudinary
        delete_uploaded_file(
            public_id=uploaded.get('public_id'),
            resource_type=uploaded.get('resource_type', 'image'),
        )
        raise



CASH_REMINDER_AFTER = timedelta(hours=2)
CASH_AUTO_CONFIRM_AFTER = timedelta(hours=24)


def _settle_cash_payment(*, booking, schedule, payment, actor=None, automatic=False):
    now = timezone.now()
    payment.status = Payment.Status.SUCCESS
    payment.paid_at = now
    payment.save(update_fields=['status', 'paid_at', 'updated_at'])  # kích hoạt signal báo khách

    booking.payment_status = Booking.PaymentStatus.PAID
    booking.save(update_fields=['payment_status', 'updated_at'])

    record_booking_activity(
        booking=booking,
        schedule=schedule,
        actor=actor,
        event_type=BookingActivity.EventType.PAYMENT_UPDATED,
        message=(
            f'Hệ thống tự xác nhận đã thu tiền mặt đơn {booking.booking_code}.'
            if automatic
            else f'Nhân viên xác nhận đã thu tiền mặt đơn {booking.booking_code}.'
        ),
        new_data={'payment_id': payment.id, 'paid_at': now.isoformat()},
        metadata={'automatic': automatic},
    )


@transaction.atomic
def confirm_cash_collected(*, schedule_id, worker):
    schedule = _get_my_accepted_schedule(schedule_id=schedule_id, worker=worker)
    booking = Booking.objects.select_for_update(of=('self',)).get(pk=schedule.booking_id)

    if schedule.status != BookingSchedule.Status.COMPLETED:
        raise serializers.ValidationError({'schedule': 'Chỉ xác nhận thu tiền sau khi đã check-out.'})

    payment = (
        Payment.objects.select_for_update()
        .filter(booking=booking, method=Payment.Method.CASH)
        .first()
    )
    if payment is None:
        raise serializers.ValidationError({'payment': 'Đơn này không thanh toán bằng tiền mặt.'})
    if payment.status == Payment.Status.SUCCESS:
        return payment  # bấm lại không lỗi, không báo khách trùng
    if payment.status != Payment.Status.PENDING:
        raise serializers.ValidationError({'payment': 'Khoản thanh toán không ở trạng thái chờ thu.'})

    if booking.status != Booking.Status.COMPLETED:
        raise serializers.ValidationError({
            'payment': 'Chỉ xác nhận thu tiền khi đơn đã hoàn thành toàn bộ các buổi.'
        })

    _settle_cash_payment(booking=booking, schedule=schedule, payment=payment, actor=worker)
    return payment


def process_pending_cash_payments():
    """Chạy định kỳ: sau 2h nhắc worker, sau 24h tự xác nhận (trừ khi có khiếu nại đang mở)."""
    from apps.complaints.models import Complaint
    from apps.notifications.models import Notification
    from apps.notifications.services import notify_worker_cash_reminder

    now = timezone.now()
    pending = Payment.objects.filter(
        method=Payment.Method.CASH,
        status=Payment.Status.PENDING,
        booking__status=Booking.Status.COMPLETED,
    ).select_related('booking')

    for payment in pending:
        schedule = (
            payment.booking.schedules
            .filter(status=BookingSchedule.Status.COMPLETED, actual_end__isnull=False)
            .order_by('-actual_end').first()
        )
        if schedule is None:
            continue
        elapsed = now - schedule.actual_end

        if elapsed >= CASH_AUTO_CONFIRM_AFTER:
            with transaction.atomic():
                booking = Booking.objects.select_for_update().get(pk=payment.booking_id)
                locked = Payment.objects.select_for_update().get(pk=payment.pk)
                if locked.status != Payment.Status.PENDING:
                    continue
                if Complaint.objects.filter(
                    booking_id=booking.id,
                    status__in=(Complaint.Status.PENDING, Complaint.Status.IN_REVIEW),
                ).exists():
                    continue
                _settle_cash_payment(
                    booking=booking, schedule=schedule, payment=locked, automatic=True,
                )
        elif elapsed >= CASH_REMINDER_AFTER:
            assignment = schedule.assignments.filter(
                status=BookingAssignment.Status.ACCEPTED,
            ).select_related('worker').first()
            if assignment is None:
                continue
            already = Notification.objects.filter(
                user=assignment.worker, related_booking=payment.booking,
                title='Nhắc xác nhận thu tiền mặt',
            ).exists()
            if not already:
                notify_worker_cash_reminder(payment.booking, schedule, assignment.worker)