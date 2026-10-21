from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.addresses.models import CustomerAddress
from apps.payments.models import Payment
from apps.worker.models import BookingAssignment

from .booking_service import create_booking
from .models import Booking, BookingActivity, BookingSchedule, BookingScheduleImage

User = get_user_model()


class AdminUserSummarySerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'full_name', 'email', 'phone_number', 'avatar']

    def get_full_name(self, obj):
        return obj.get_full_name() or obj.username


class AdminAddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerAddress
        fields = [
            'id', 'label', 'receiver_name', 'receiver_phone', 'address_line',
            'ward', 'city', 'latitude', 'longitude', 'is_default',
        ]


class AdminWorkerSummarySerializer(AdminUserSummarySerializer):
    average_rating = serializers.SerializerMethodField()
    total_completed_jobs = serializers.SerializerMethodField()
    registered_service_id = serializers.SerializerMethodField()

    class Meta(AdminUserSummarySerializer.Meta):
        fields = AdminUserSummarySerializer.Meta.fields + [
            'average_rating', 'total_completed_jobs', 'registered_service_id',
        ]

    def _profile(self, obj):
        return getattr(obj, 'worker_profile', None)

    def get_average_rating(self, obj):
        profile = self._profile(obj)
        return str(profile.average_rating) if profile else None

    def get_total_completed_jobs(self, obj):
        profile = self._profile(obj)
        return profile.total_completed_jobs if profile else 0

    def get_registered_service_id(self, obj):
        profile = self._profile(obj)
        return profile.registered_service_id if profile else None


class AdminAssignmentSerializer(serializers.ModelSerializer):
    worker = AdminWorkerSummarySerializer(read_only=True)
    assigned_by = AdminUserSummarySerializer(read_only=True)
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    assigned_method_label = serializers.CharField(source='get_assigned_method_display', read_only=True)

    class Meta:
        model = BookingAssignment
        fields = [
            'id', 'worker', 'assigned_by', 'assigned_method',
            'assigned_method_label', 'status', 'status_label', 'response_note',
            'assigned_at', 'responded_at', 'expired_at', 'commission_reserved',
        ]


class AdminScheduleImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingScheduleImage
        fields = ['id', 'image_type', 'image', 'note', 'sort_order', 'created_at']


class AdminScheduleSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    current_assignment = serializers.SerializerMethodField()
    assignment_history = serializers.SerializerMethodField()
    invitation = serializers.SerializerMethodField()
    images = AdminScheduleImageSerializer(many=True, read_only=True)

    class Meta:
        model = BookingSchedule
        fields = [
            'id', 'sequence_no', 'scheduled_start', 'scheduled_end',
            'actual_start', 'actual_end', 'status', 'status_label', 'note',
            'completion_note', 'cancel_reason', 'current_assignment',
            'assignment_history', 'images', 'invitation',
        ]

    def _assignments(self, obj):
        prefetched = getattr(obj, 'admin_assignments', None)
        if prefetched is not None:
            return prefetched
        return list(
            obj.assignments.select_related(
                'worker', 'worker__worker_profile', 'assigned_by',
            ).order_by('-assigned_at', '-id')
        )

    def get_current_assignment(self, obj):
        assignment = next(
            (a for a in self._assignments(obj) if a.status == BookingAssignment.Status.ACCEPTED),
            None,
        )
        return AdminAssignmentSerializer(assignment).data if assignment else None

    def get_invitation(self, obj):
        from django.utils import timezone
        now = timezone.now()
        if obj.status != 'PENDING' or obj.booking.status not in ('PENDING', 'ASSIGNED', 'IN_PROGRESS'):
            return None
        pending = [a for a in self._assignments(obj) if a.status == 'PENDING' and a.expired_at and a.expired_at > now]
        if pending:
            return {'source': 'ADMIN', 'worker': AdminWorkerSummarySerializer(pending[0].worker).data,
                    'workers': AdminWorkerSummarySerializer([a.worker for a in pending], many=True).data,
                    'pending_count': len(pending), 'expires_at': min(a.expired_at for a in pending), 'id': pending[0].id}
        if obj.preferred_worker_id and obj.preferred_worker_expires_at and obj.preferred_worker_expires_at > now and not self.get_current_assignment(obj):
            return {'source': 'CUSTOMER', 'worker': AdminWorkerSummarySerializer(obj.preferred_worker).data,
                    'expires_at': obj.preferred_worker_expires_at, 'id': None}
        return None

    def get_assignment_history(self, obj):
        return AdminAssignmentSerializer(self._assignments(obj), many=True).data


class AdminPaymentSerializer(serializers.ModelSerializer):
    method_label = serializers.CharField(source='get_method_display', read_only=True)
    status_label = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Payment
        fields = [
            'id', 'amount', 'method', 'method_label', 'status', 'status_label',
            'transaction_code', 'paid_at', 'failure_reason', 'created_at',
        ]


class AdminBookingListSerializer(serializers.ModelSerializer):
    customer = AdminUserSummarySerializer(read_only=True)
    service = serializers.SerializerMethodField()
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    payment_status_label = serializers.CharField(source='get_payment_status_display', read_only=True)
    total_schedules = serializers.IntegerField(read_only=True)
    assigned_schedules = serializers.IntegerField(read_only=True)
    waiting_invitations = serializers.SerializerMethodField()
    completed_schedules = serializers.IntegerField(read_only=True)
    next_schedule_start = serializers.DateTimeField(read_only=True)
    workers = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            'id', 'booking_code', 'customer', 'service', 'status', 'status_label',
            'payment_status', 'payment_status_label', 'total_amount',
            'total_schedules', 'assigned_schedules', 'completed_schedules', 'waiting_invitations',
            'next_schedule_start', 'workers', 'created_at', 'updated_at',
        ]

    def get_waiting_invitations(self, obj):
        from django.db.models import Exists, OuterRef, Q
        from django.utils import timezone
        pending = BookingAssignment.objects.filter(schedule_id=OuterRef('pk'), status='PENDING', expired_at__gt=timezone.now())
        accepted = BookingAssignment.objects.filter(schedule_id=OuterRef('pk'), status='ACCEPTED')
        return obj.schedules.filter(status='PENDING').filter(
            Q(preferred_worker__isnull=False, preferred_worker_expires_at__gt=timezone.now()) | Exists(pending)
        ).exclude(Exists(accepted)).count()

    def get_service(self, obj):
        return {'id': obj.service_id, 'code': obj.service.code, 'name': obj.service.name}

    def get_workers(self, obj):
        seen = set()
        workers = []
        for schedule in obj.schedules.all():
            for assignment in getattr(schedule, 'accepted_admin_assignments', []):
                if assignment.worker_id not in seen:
                    seen.add(assignment.worker_id)
                    workers.append(assignment.worker)
        return AdminWorkerSummarySerializer(workers, many=True).data


class AdminBookingDetailSerializer(serializers.ModelSerializer):
    customer = AdminUserSummarySerializer(read_only=True)
    service = serializers.SerializerMethodField()
    address = AdminAddressSerializer(read_only=True)
    delivery_address = AdminAddressSerializer(read_only=True)
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    payment_status_label = serializers.CharField(source='get_payment_status_display', read_only=True)
    voucher = serializers.SerializerMethodField()
    schedules = AdminScheduleSerializer(many=True, read_only=True)
    payments = AdminPaymentSerializer(many=True, read_only=True)
    complaints = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            'id', 'booking_code', 'customer', 'service', 'service_data',
            'address', 'delivery_address', 'note', 'status', 'status_label',
            'payment_status', 'payment_status_label', 'price_breakdown',
            'subtotal_amount', 'discount_amount', 'total_amount',
            'refunded_amount', 'voucher',
            'schedules', 'payments', 'complaints', 'cancelled_by',
            'cancelled_at', 'cancel_reason', 'created_at', 'updated_at',
        ]

    def get_service(self, obj):
        return {
            'id': obj.service_id,
            'code': obj.service.code,
            'section_code': obj.service.section_code,
            'name': obj.service.name,
            'form_schema': obj.service.form_schema,
        }

    def get_voucher(self, obj):
        if not obj.user_voucher_id:
            return None
        voucher = obj.user_voucher.voucher
        return {'id': voucher.id, 'code': voucher.code, 'name': voucher.name}

    def get_complaints(self, obj):
        return [
            {
                'id': complaint.id,
                'code': complaint.code,
                'schedule_id': complaint.schedule_id,
                'stage': complaint.stage,
                'status': complaint.status,
                'issue_type': complaint.issue_type.name,
                'created_at': complaint.created_at,
            }
            for complaint in obj.complaints.all()
        ]


class AdminBookingCreateSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField()
    service_id = serializers.IntegerField()
    address_id = serializers.IntegerField()
    delivery_address_id = serializers.IntegerField(required=False, allow_null=True)
    service_data = serializers.JSONField()
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    voucher_code = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    payment_method = serializers.ChoiceField(choices=[Payment.Method.CASH, Payment.Method.BANK_TRANSFER])

    def validate_customer_id(self, value):
        if not User.objects.filter(pk=value, role='CUSTOMER', is_active=True).exists():
            raise serializers.ValidationError('Khách hàng không tồn tại hoặc đã bị khóa.')
        return value

    def create(self, validated_data):
        customer = User.objects.get(pk=validated_data.pop('customer_id'))
        return create_booking(
            customer=customer,
            actor=self.context['request'].user,
            **validated_data,
        )


class AdminBookingUpdateSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    address_id = serializers.IntegerField(required=False)
    delivery_address_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError('Không có dữ liệu cần cập nhật.')
        return attrs


class AdminScheduleUpdateSerializer(serializers.Serializer):
    scheduled_start = serializers.DateTimeField(required=False)
    scheduled_end = serializers.DateTimeField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=False, max_length=500)

    def validate(self, attrs):
        if not any(key in attrs for key in ('scheduled_start', 'scheduled_end', 'note')):
            raise serializers.ValidationError('Không có dữ liệu cần cập nhật.')
        if any(key in attrs for key in ('scheduled_start', 'scheduled_end')) and not attrs.get('reason'):
            raise serializers.ValidationError({'reason': 'Vui lòng nhập lý do đổi lịch.'})
        return attrs


class AdminAssignWorkerSerializer(serializers.Serializer):
    response_minutes = serializers.ChoiceField(choices=[15, 30, 60], default=15)
    worker_id = serializers.IntegerField()


class AdminBulkAssignSerializer(serializers.Serializer):
    response_minutes = serializers.ChoiceField(choices=[15, 30, 60], default=15)
    booking_id = serializers.IntegerField()
    schedule_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)
    worker_id = serializers.IntegerField(required=False)
    worker_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, allow_empty=False, max_length=100)

    def validate_schedule_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError('Danh sách buổi bị trùng lặp.')
        return value


    def validate(self, attrs):
        if ('worker_id' in attrs) == ('worker_ids' in attrs):
            raise serializers.ValidationError({'worker_ids': 'Chọn worker_id hoặc worker_ids.'})
        if 'worker_ids' in attrs and len(set(attrs['worker_ids'])) != len(attrs['worker_ids']):
            raise serializers.ValidationError({'worker_ids': 'Danh sách nhân viên bị trùng lặp.'})
        return attrs

class AdminReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, trim_whitespace=True)


class AdminCompleteScheduleSerializer(AdminReasonSerializer):
    completion_note = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class BookingActivitySerializer(serializers.ModelSerializer):
    actor = AdminUserSummarySerializer(read_only=True)
    event_type_label = serializers.CharField(source='get_event_type_display', read_only=True)
    schedule_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = BookingActivity
        fields = [
            'id', 'event_type', 'event_type_label', 'message', 'actor',
            'schedule_id', 'old_data', 'new_data', 'metadata', 'created_at',
        ]


class AdminCustomerSearchSerializer(AdminUserSummarySerializer):
    addresses = AdminAddressSerializer(many=True, read_only=True)

    class Meta(AdminUserSummarySerializer.Meta):
        fields = AdminUserSummarySerializer.Meta.fields + ['is_active', 'addresses']


class AdminAvailableWorkerSerializer(AdminWorkerSummarySerializer):
    is_customer_favorite = serializers.BooleanField(read_only=True, default=False)
    is_customer_requested = serializers.BooleanField(read_only=True, default=False)
    can_receive_invitation = serializers.BooleanField(read_only=True, default=True)
    unavailable_reasons = serializers.ListField(child=serializers.CharField(), read_only=True)
    active_jobs_count = serializers.IntegerField(read_only=True)
    matched_area = serializers.BooleanField(read_only=True, default=True)
    has_time_conflict = serializers.BooleanField(read_only=True, default=False)
    cash_balance_eligible = serializers.BooleanField(read_only=True, default=True)
    bio = serializers.CharField(source='worker_profile.bio', read_only=True)
    experience_years = serializers.IntegerField(source='worker_profile.experience_years', read_only=True)
    gender = serializers.CharField(read_only=True)
    registered_service = serializers.SerializerMethodField()
    working_areas = serializers.SerializerMethodField()

    def get_registered_service(self, obj):
        service = getattr(getattr(obj, 'worker_profile', None), 'registered_service', None)
        if not service:
            return None
        return {'id': service.id, 'code': service.code, 'name': service.name}

    def get_working_areas(self, obj):
        return [
            {'id': row.area_id, 'name': row.area.name, 'city': row.area.city}
            for row in obj.working_areas.all()
            if row.area.is_active
        ]

    class Meta(AdminWorkerSummarySerializer.Meta):
        fields = AdminWorkerSummarySerializer.Meta.fields + [
            'is_customer_favorite', 'is_customer_requested', 'can_receive_invitation', 'unavailable_reasons',
            'active_jobs_count', 'matched_area', 'has_time_conflict',
            'cash_balance_eligible', 'bio', 'experience_years', 'gender',
            'registered_service', 'working_areas',
        ]
