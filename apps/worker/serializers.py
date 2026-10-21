from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.bookings.models import BookingSchedule, BookingScheduleImage
from apps.payments.models import Payment

from . import assignment_service
from .constants import MIN_CANCEL_HOURS
from .models import Area, BookingAssignment, CustomerFavoriteWorker, WorkerWorkingArea


User = get_user_model()


def _worker_avatar(worker):
    profile = getattr(worker, 'worker_profile', None)
    return profile.avatar if profile and profile.avatar else worker.avatar


class CustomerWorkerProfileSerializer(serializers.ModelSerializer):
    worker_id = serializers.IntegerField(source='id', read_only=True)
    avatar = serializers.SerializerMethodField()
    bio = serializers.CharField(source='worker_profile.bio', read_only=True, allow_null=True)
    experience_years = serializers.IntegerField(source='worker_profile.experience_years', read_only=True)
    average_rating = serializers.DecimalField(
        source='worker_profile.average_rating',
        max_digits=3,
        decimal_places=2,
        read_only=True,
    )
    total_completed_jobs = serializers.IntegerField(
        source='worker_profile.total_completed_jobs',
        read_only=True,
    )
    is_favorite = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'worker_id', 'first_name', 'last_name', 'avatar', 'bio',
            'experience_years', 'average_rating', 'total_completed_jobs',
            'is_favorite',
        ]
        read_only_fields = fields

    def get_avatar(self, worker):
        return _worker_avatar(worker)

    def get_is_favorite(self, worker):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        return CustomerFavoriteWorker.objects.filter(
            customer=request.user,
            worker=worker,
        ).exists()


class FavoriteWorkerSerializer(serializers.ModelSerializer):
    worker_id = serializers.IntegerField(source='worker.id', read_only=True)
    first_name = serializers.CharField(source='worker.first_name', read_only=True)
    last_name = serializers.CharField(source='worker.last_name', read_only=True)
    avatar = serializers.SerializerMethodField()
    bio = serializers.CharField(source='worker.worker_profile.bio', read_only=True, allow_null=True)
    experience_years = serializers.IntegerField(source='worker.worker_profile.experience_years', read_only=True)
    average_rating = serializers.DecimalField(
        source='worker.worker_profile.average_rating',
        max_digits=3,
        decimal_places=2,
        read_only=True,
    )
    total_completed_jobs = serializers.IntegerField(
        source='worker.worker_profile.total_completed_jobs',
        read_only=True,
    )
    is_favorite = serializers.BooleanField(default=True, read_only=True)

    class Meta:
        model = CustomerFavoriteWorker
        fields = [
            'worker_id', 'first_name', 'last_name', 'avatar', 'bio',
            'experience_years', 'average_rating', 'total_completed_jobs',
            'is_favorite', 'created_at',
        ]
        read_only_fields = fields

    def get_avatar(self, favorite):
        return _worker_avatar(favorite.worker)


class AreaSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Area
        fields = ['id', 'name', 'city', 'province_code', 'ward_code']


class WorkerWorkingAreaSerializer(serializers.ModelSerializer):
    """Chỉ dùng để serialize output (GET, và trả về sau khi PUT)."""
    area = AreaSummarySerializer(read_only=True)

    class Meta:
        model = WorkerWorkingArea
        fields = ['id', 'area', 'created_at']
        read_only_fields = fields


class WorkerWorkingAreaBulkUpdateSerializer(serializers.Serializer):
    area_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Area.objects.filter(is_active=True),
        allow_empty=False,
        error_messages={
            'empty': 'Phải chọn ít nhất một khu vực hoạt động.',
            'does_not_exist': 'Khu vực với id={pk_value} không tồn tại hoặc không còn hoạt động.',
        },
    )

    def validate_area_ids(self, value):
        ids = [area.id for area in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError('Danh sách khu vực bị trùng lặp.')
        if len({area.province_code for area in value}) > 1:
            raise serializers.ValidationError('Chỉ được chọn khu vực trong cùng 1 tỉnh/thành.')
        return value

    def save(self):
        worker = self.context['request'].user
        areas = self.validated_data['area_ids']
        area_ids = [area.id for area in areas]

        with transaction.atomic():
            WorkerWorkingArea.objects.filter(worker=worker).exclude(area_id__in=area_ids).delete()

            existing_ids = set(
                WorkerWorkingArea.objects.filter(worker=worker, area_id__in=area_ids)
                .values_list('area_id', flat=True)
            )
            to_create = [
                WorkerWorkingArea(worker=worker, area_id=aid)
                for aid in area_ids
                if aid not in existing_ids
            ]
            if to_create:
                WorkerWorkingArea.objects.bulk_create(to_create)

        return WorkerWorkingArea.objects.filter(worker=worker).select_related('area').order_by('area__city', 'area__name')


class BookingScheduleImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingScheduleImage
        fields = ['id', 'image', 'image_type', 'note', 'created_at']
        read_only_fields = fields


class WorkerScheduleSerializer(serializers.ModelSerializer):
    booking_id = serializers.IntegerField(read_only=True)
    booking_code = serializers.CharField(source='booking.booking_code', read_only=True)
    service_id = serializers.IntegerField(source='booking.service_id', read_only=True)
    service_name = serializers.CharField(source='booking.service.name', read_only=True)
    booking_note = serializers.CharField(source='booking.note', read_only=True, allow_null=True)
    total_sessions = serializers.IntegerField(read_only=True)
    address_city = serializers.CharField(source='booking.address.city', read_only=True)
    address_ward = serializers.CharField(source='booking.address.ward', read_only=True)
    payment_method = serializers.SerializerMethodField()
    cash_to_collect = serializers.SerializerMethodField()
    platform_fee = serializers.SerializerMethodField()
    address_latitude = serializers.DecimalField(
        source='booking.address.latitude', read_only=True, allow_null=True,
        max_digits=14, decimal_places=7,
    )
    address_longitude = serializers.DecimalField(
        source='booking.address.longitude', read_only=True, allow_null=True,
        max_digits=14, decimal_places=7,
    )
    delivery_city = serializers.CharField(
        source='booking.delivery_address.city', read_only=True, allow_null=True)
    delivery_ward = serializers.CharField(
        source='booking.delivery_address.ward', read_only=True, allow_null=True)
    customer_avatar = serializers.CharField(source='booking.customer.avatar', read_only=True, allow_null=True)
    customer_name = serializers.SerializerMethodField()
    payment_status = serializers.CharField(source='booking.payment_status', read_only=True)
    price = serializers.SerializerMethodField()
    service_data = serializers.JSONField(source='booking.service_data', read_only=True)
    assignment_id = serializers.SerializerMethodField()
    available_sessions = serializers.SerializerMethodField()
    is_preferred_for_me = serializers.SerializerMethodField()
    preferred_until = serializers.SerializerMethodField()
    invitation = serializers.SerializerMethodField()

    def get_payment_method(self, obj):
        payments = sorted(obj.booking.payments.all(), key=lambda p: p.created_at, reverse=True)
        return payments[0].method if payments else None

    def _pending_cash_payment(self, obj):
        # Dùng booking__payments đã prefetch nên không phát sinh query thêm.
        return next(
            (p for p in obj.booking.payments.all()
             if p.method == Payment.Method.CASH and p.status == Payment.Status.PENDING),
            None,
        )

    def get_cash_to_collect(self, obj):
        payment = self._pending_cash_payment(obj)
        if payment is None:
            return None
        return str(payment.amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP))

    def get_platform_fee(self, obj):
        # Đơn tiền mặt: hoa hồng bị trừ ví ngay lúc nhận việc (reserve_cash_commission).
        if self._pending_cash_payment(obj) is None:
            return None
        from apps.wallets import earning_service  # import trong hàm để tránh vòng import
        gross = earning_service.calculate_gross_amount(obj, obj.booking)
        fee = earning_service.calculate_commission(gross)
        return str(fee) if fee > 0 else None

    def get_invitation(self, instance):
        request = self.context.get('request')
        worker_id = getattr(getattr(request, 'user', None), 'id', None)
        invitations = [a for a in instance.assignments.all() if a.worker_id == worker_id and a.assigned_by_id and a.expired_at]
        if not invitations:
            return None
        invitation = max(invitations, key=lambda a: a.id)
        status = invitation.status
        if status == 'PENDING' and (invitation.expired_at <= timezone.now() or instance.status != 'PENDING' or instance.booking.status not in ('PENDING', 'ASSIGNED', 'IN_PROGRESS')):
            status = 'EXPIRED'
        return {'id': invitation.id, 'source': 'ADMIN', 'status': status,
                'expires_at': invitation.expired_at,
                'response_note': invitation.response_note or '',
                'sender_name': invitation.assigned_by.get_full_name() or invitation.assigned_by.username}

    def get_is_preferred_for_me(self, instance):
        request = self.context.get('request')
        worker_id = getattr(getattr(request, 'user', None), 'id', None)
        return bool(worker_id and instance.preferred_worker_id == worker_id)

    def get_preferred_until(self, instance):
        if not self.get_is_preferred_for_me(instance):
            return None
        expires_at = instance.preferred_worker_expires_at
        return expires_at if expires_at and expires_at > timezone.now() else None

    def get_available_sessions(self, instance):
        # Chỉ có giá trị khi list được gộp theo booking (group_by=booking); còn lại None.
        return getattr(instance, 'available_sessions', None)

    class Meta:
        model = BookingSchedule
        fields = [
            'id', 'booking_id', 'booking_code', 'service_id', 'service_name', 'booking_note',
            'sequence_no', 'total_sessions', 'scheduled_start', 'scheduled_end', 'status',
            'address_city', 'address_ward', 'address_latitude', 'address_longitude',
            'customer_avatar', 'customer_name', 'payment_status', 'price',
            'service_data', 'assignment_id', 'available_sessions', 'delivery_city', 'delivery_ward',
            'is_preferred_for_me', 'preferred_until', 'invitation',
            'payment_method', 'cash_to_collect', 'platform_fee',
        ]
        read_only_fields = fields

    def get_customer_name(self, instance):
        customer = instance.booking.customer
        full_name = f'{customer.first_name} {customer.last_name}'.strip()
        return full_name or customer.username

    def get_assignment_id(self, instance):
        assignment = next(
            (a for a in instance.assignments.all() if a.status == BookingAssignment.Status.ACCEPTED),
            None,
        )
        return assignment.id if assignment else None

    def get_price(self, instance):
        # Ưu tiên unit_price đã chốt trong price_breakdown lúc tạo booking
        # (giá gốc/buổi, không bị trừ theo giảm giá gói dài hay voucher của khách).
        # Fallback: booking cũ chưa có unit_price -> chia đều total_amount cho total_sessions.
        breakdown = instance.booking.price_breakdown or {}
        unit_price = breakdown.get('unit_price')
        if unit_price is not None:
            try:
                return str(Decimal(str(unit_price)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            except (TypeError, ValueError, ArithmeticError):
                pass

        total = instance.booking.total_amount
        sessions = getattr(instance, 'total_sessions', None)
        if total is None or not sessions:
            return None
        per_session = (total / sessions).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        return str(per_session)


class WorkerMyScheduleSerializer(WorkerScheduleSerializer):
    """my-schedules: lộ thông tin liên hệ + ảnh vì nhân viên đã nhận việc."""
    address_line = serializers.CharField(source='booking.address.address_line', read_only=True)
    receiver_name = serializers.CharField(source='booking.address.receiver_name', read_only=True)
    receiver_phone = serializers.CharField(source='booking.address.receiver_phone', read_only=True)
    can_cancel = serializers.SerializerMethodField()
    cancel_deadline = serializers.SerializerMethodField()
    completed_sessions = serializers.SerializerMethodField()
    accepted_sessions = serializers.SerializerMethodField()
    booking_status = serializers.CharField(source='booking.status', read_only=True)
    images = BookingScheduleImageSerializer(many=True, read_only=True)
    form_schema = serializers.JSONField(source='booking.service.form_schema', read_only=True)
    delivery_address_line = serializers.CharField(
        source='booking.delivery_address.address_line', read_only=True, allow_null=True)

    class Meta(WorkerScheduleSerializer.Meta):
        fields = WorkerScheduleSerializer.Meta.fields + [
            'form_schema', 'note', 'cancel_reason', 'address_line', 'receiver_name', 'receiver_phone',
            'can_cancel', 'cancel_deadline', 'images', 'completion_note', 'delivery_address_line',
            'completed_sessions', 'accepted_sessions',
            'booking_status',
        ]
        read_only_fields = fields

    def get_completed_sessions(self, obj):
        return getattr(obj, 'completed_sessions', None)

    def get_accepted_sessions(self, obj):
        return getattr(obj, 'accepted_sessions', None)

    def get_can_cancel(self, obj):
        return (
            obj.status == BookingSchedule.Status.PENDING
            and assignment_service.get_cancel_deadline(obj) > timezone.now()
        )

    def get_cancel_deadline(self, obj):
        return assignment_service.get_cancel_deadline(obj)


class WorkerBookingScheduleSerializer(WorkerScheduleSerializer):
    """
    Dùng cho GET /bookings/<id>/schedules/ — toàn bộ buổi của 1 gói.
    claim_state: OPEN (còn trống) / MINE (worker đang gọi API đã nhận) /
    TAKEN (người khác đã nhận — KHÔNG trả tên/id người đó, chỉ trạng thái,
    để bảo mật thông tin nhân viên khác).
    """
    claim_state = serializers.SerializerMethodField()
    form_schema = serializers.JSONField(source='booking.service.form_schema', read_only=True)

    class Meta(WorkerScheduleSerializer.Meta):
        fields = WorkerScheduleSerializer.Meta.fields + ['form_schema', 'claim_state']
        read_only_fields = fields

    def _accepted_assignment(self, instance):
        return next(
            (a for a in instance.assignments.all() if a.status == BookingAssignment.Status.ACCEPTED),
            None,
        )

    def get_assignment_id(self, instance):
        # Ghi đè: chỉ trả assignment_id nếu buổi đó là CỦA WORKER ĐANG GỌI API,
        # để FE không nhầm assignment của người khác thành của mình khi hiện nút hủy.
        request = self.context.get('request')
        worker_id = getattr(getattr(request, 'user', None), 'id', None)
        assignment = self._accepted_assignment(instance)
        if assignment and assignment.worker_id == worker_id:
            return assignment.id
        return None

    def get_claim_state(self, instance):
        request = self.context.get('request')
        worker = getattr(request, 'user', None)
        assignment = self._accepted_assignment(instance)

        if assignment is not None:
            return 'MINE' if assignment.worker_id == getattr(worker, 'id', None) else 'TAKEN'

        from .invitation_service import active_invitation
        invitation = active_invitation(instance)
        if invitation and not active_invitation(instance, worker):
            return 'TAKEN'
        if (instance.preferred_worker_id and instance.preferred_worker_id != getattr(worker, 'id', None)
                and instance.preferred_worker_expires_at and instance.preferred_worker_expires_at > timezone.now()):
            return 'TAKEN'
        if instance.status != 'PENDING' or instance.scheduled_start <= timezone.now() or instance.booking.status not in ('PENDING', 'ASSIGNED', 'IN_PROGRESS'):
            return 'TAKEN'
        # Buổi còn trống nhưng trùng giờ với buổi khác worker đã nhận -> CONFLICT
        if worker is not None and assignment_service._has_time_conflict(worker, instance):
            return 'CONFLICT'

        return 'OPEN'


class CancelAssignmentSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, trim_whitespace=True)


class ClaimBookingPackageSerializer(serializers.Serializer):
    """
    Body cho POST /bookings/<booking_id>/claim/.
    - Không gửi `schedule_ids` -> nhận toàn bộ buổi PENDING còn trống của booking.
    - Gửi `schedule_ids` -> chỉ nhận đúng các buổi đó.
    """
    schedule_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=False,
        error_messages={'empty': 'Danh sách buổi không được để trống.'},
    )

    def validate_schedule_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError('Danh sách buổi bị trùng lặp.')
        return value


class AdminAssignWorkerSerializer(serializers.Serializer):
    response_minutes = serializers.ChoiceField(choices=[15, 30, 60], default=15)
    worker_id = serializers.IntegerField()


class ScheduleImageUploadSerializer(serializers.Serializer):
    image = serializers.ImageField()
    image_type = serializers.ChoiceField(choices=BookingScheduleImage.ImageType.choices)
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class CheckOutSerializer(serializers.Serializer):
    completion_note = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class CheckInSerializer(serializers.Serializer):
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    accuracy = serializers.FloatField(required=False, allow_null=True, min_value=0)