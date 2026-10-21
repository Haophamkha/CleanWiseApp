# apps/complaints/serializers.py

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from apps.bookings.activity_service import record_booking_activity
from apps.bookings.models import Booking, BookingActivity, BookingSchedule
from apps.common.permissions import get_user_role
from apps.worker.constants import AUTO_CANCEL_NOTE
from apps.worker.models import BookingAssignment

from .models import (
    Complaint,
    ComplaintAttachment,
    ComplaintIssueType,
)

WORKED_AUTO_CANCELLED_CODE = 'AUTO_CANCELLED_WORKED'
AUTO_CANCEL_COMPLAINT_HOURS = getattr(settings, 'AUTO_CANCEL_COMPLAINT_HOURS', 72)


class ComplaintIssueTypeSerializer(serializers.ModelSerializer):
    stage_label = serializers.CharField(source='get_stage_display', read_only=True)

    class Meta:
        model = ComplaintIssueType
        fields = [
            'id', 'code', 'name', 'description', 'stage', 'stage_label',
            'applies_to', 'is_active',
        ]
        read_only_fields = ['id', 'stage_label']


class ComplaintAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ComplaintAttachment
        fields = ['id', 'file', 'file_type', 'created_at']
        read_only_fields = ['id', 'file_type', 'created_at']

    def validate_file(self, value):
        allowed = ('image/jpeg', 'image/png', 'image/webp')
        if value.content_type not in allowed:
            raise serializers.ValidationError('Chỉ chấp nhận file ảnh (jpeg/png/webp).')
        if value.size > 5 * 1024 * 1024:
            raise serializers.ValidationError('Ảnh tối đa 5MB.')
        return value


class ComplaintCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Complaint
        fields = ['id', 'code', 'booking', 'schedule', 'issue_type', 'content']
        read_only_fields = ['id', 'code']

    def validate_booking(self, booking):
        request = self.context['request']
        role = get_user_role(request.user)

        if role == 'CUSTOMER':
            if booking.customer_id != request.user.id:
                raise serializers.ValidationError('Booking không thuộc về bạn.')
        elif role == 'WORKER':
            had = BookingAssignment.objects.filter(
                schedule__booking=booking, worker=request.user,
            ).filter(
                Q(status=BookingAssignment.Status.ACCEPTED)
                | Q(status=BookingAssignment.Status.CANCELLED, response_note=AUTO_CANCEL_NOTE)
            ).exists()
            if not had:
                raise serializers.ValidationError('Bạn chưa từng nhận buổi làm nào thuộc đơn này.')
        else:
            raise serializers.ValidationError('Vai trò của bạn không được phép tạo khiếu nại.')

        # Kiểm tra đơn CANCELLED/FAILED nằm ở validate() vì phụ thuộc loại sự cố.
        return booking

    def validate_issue_type(self, issue_type):
        if not issue_type.is_active:
            raise serializers.ValidationError('Loại sự cố này hiện không còn được sử dụng.')

        role = get_user_role(self.context['request'].user)
        if issue_type.applies_to not in (role, ComplaintIssueType.AppliesTo.ANY):
            raise serializers.ValidationError('Loại sự cố này không áp dụng cho vai trò của bạn.')
        return issue_type

    @staticmethod
    def _validate_worked_claim(*, user, role, schedule):
        """Nhân viên đã làm nhưng buổi bị hệ thống tự hủy vì không check-in."""
        if role != 'WORKER':
            raise serializers.ValidationError({'issue_type': 'Loại sự cố này chỉ dành cho nhân viên.'})
        if schedule is None:
            raise serializers.ValidationError({'schedule': 'Vui lòng chọn buổi làm việc cụ thể.'})
        if schedule.status != BookingSchedule.Status.MISSED:
            raise serializers.ValidationError({'schedule': 'Chỉ áp dụng cho buổi bị hệ thống tự hủy.'})
        if schedule.scheduled_end < timezone.now() - timedelta(hours=AUTO_CANCEL_COMPLAINT_HOURS):
            raise serializers.ValidationError({
                'schedule': f'Đã quá {AUTO_CANCEL_COMPLAINT_HOURS} giờ, không thể khiếu nại buổi này.'
            })
        if not BookingAssignment.objects.filter(
            schedule=schedule, worker=user,
            status=BookingAssignment.Status.CANCELLED, response_note=AUTO_CANCEL_NOTE,
        ).exists():
            raise serializers.ValidationError({
                'schedule': 'Buổi này không bị tự hủy do bạn không check-in.'
            })

    def validate(self, attrs):
        request = self.context['request']
        user = request.user
        role = get_user_role(user)
        booking = attrs['booking']
        schedule = attrs.get('schedule')
        issue_type = attrs['issue_type']
        worked_claim = issue_type.code == WORKED_AUTO_CANCELLED_CODE

        if schedule and schedule.booking_id != booking.id:
            raise serializers.ValidationError({'schedule': 'Buổi làm việc không khớp với booking.'})

        if worked_claim:
            self._validate_worked_claim(user=user, role=role, schedule=schedule)
        elif booking.status in (Booking.Status.CANCELLED, Booking.Status.FAILED):
            raise serializers.ValidationError(
                'Không thể tạo khiếu nại cho booking đã bị hủy hoặc thất bại.'
            )
        elif role == 'WORKER':
            # Nhân viên bắt buộc gắn đúng buổi mình đang nhận để suy ra `worker`.
            if not schedule:
                raise serializers.ValidationError({'schedule': 'Vui lòng chọn buổi làm việc cụ thể.'})
            if not BookingAssignment.objects.filter(
                schedule=schedule, worker=user, status=BookingAssignment.Status.ACCEPTED,
            ).exists():
                raise serializers.ValidationError({'schedule': 'Buổi làm việc này không thuộc về bạn.'})

        stage = self._stage_for(booking, issue_type)
        if issue_type.stage != ComplaintIssueType.Stage.ANY and issue_type.stage != stage:
            raise serializers.ValidationError({
                'issue_type': 'Loại sự cố này không phù hợp với trạng thái hiện tại của booking.'
            })

        # Khóa schedule trước khi check trùng để 2 request song song bị serialize.
        # Bọc atomic riêng để chạy được cả khi gọi ngoài transaction (chatbot);
        # trong view (đã có atomic) thì thành savepoint, vẫn giữ khóa đến hết transaction.
        if schedule:
            with transaction.atomic():
                BookingSchedule.objects.select_for_update(of=('self',)).get(pk=schedule.pk)
                if Complaint.objects.filter(
                    schedule=schedule, reporter=user,
                ).exclude(status=Complaint.Status.CANCELLED).exists():
                    raise serializers.ValidationError({
                        'schedule': 'Bạn đã tạo khiếu nại cho buổi làm việc này rồi.'
                    })

        return attrs

    @staticmethod
    def _get_booking_stage(booking):
        if booking.status in [booking.Status.PENDING, booking.Status.ASSIGNED]:
            return Complaint.Stage.BEFORE_SERVICE
        if booking.status == booking.Status.IN_PROGRESS:
            return Complaint.Stage.IN_SERVICE
        if booking.status == booking.Status.COMPLETED:
            return Complaint.Stage.AFTER_SERVICE
        raise serializers.ValidationError('Booking hiện tại không cho phép tạo khiếu nại.')

    def _stage_for(self, booking, issue_type):
        if issue_type.code == WORKED_AUTO_CANCELLED_CODE:
            return Complaint.Stage.AFTER_SERVICE
        return self._get_booking_stage(booking)

    def _infer_worker(self, *, role, user, schedule):
        if role == 'WORKER':
            return user
        if schedule:
            assignment = BookingAssignment.objects.filter(
                schedule=schedule, status=BookingAssignment.Status.ACCEPTED,
            ).select_related('worker').first()
            if assignment:
                return assignment.worker
        return None

    def create(self, validated_data):
        request = self.context['request']
        role = get_user_role(request.user)
        booking = validated_data['booking']
        schedule = validated_data.get('schedule')
        stage = self._stage_for(booking, validated_data['issue_type'])
        worker = self._infer_worker(role=role, user=request.user, schedule=schedule)

        # complaint.code được Complaint.save() tự sinh.
        complaint = Complaint.objects.create(
            reporter=request.user,
            reporter_role=role,
            worker=worker,
            stage=stage,
            **validated_data,
        )
        record_booking_activity(
            booking=booking,
            schedule=complaint.schedule,
            actor=request.user,
            event_type=BookingActivity.EventType.COMPLAINT_CREATED,
            message=f'{"Nhân viên" if role == "WORKER" else "Khách hàng"} tạo khiếu nại {complaint.code}.',
            new_data={'complaint_id': complaint.id, 'code': complaint.code, 'stage': complaint.stage},
        )
        return complaint
    
class ComplaintListSerializer(serializers.ModelSerializer):
    booking_code = serializers.CharField(source='booking.booking_code', read_only=True)
    schedule_sequence_no = serializers.IntegerField(
        source='schedule.sequence_no', read_only=True, default=None,
    )
    issue_type_name = serializers.CharField(source='issue_type.name', read_only=True)
    issue_type_code = serializers.CharField(source='issue_type.code', read_only=True)
    stage_label = serializers.CharField(source='get_stage_display', read_only=True)
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    reporter_name = serializers.CharField(source='reporter.get_full_name', read_only=True)
    worker_name = serializers.CharField(source='worker.get_full_name', read_only=True, default=None)

    class Meta:
        model = Complaint
        fields = [
            'id', 'code', 'booking', 'booking_code', 'schedule', 'schedule_sequence_no',
            'reporter_role', 'issue_type', 'issue_type_code', 'issue_type_name',
            'stage', 'stage_label', 'status',
            'reporter_name', 'worker', 'worker_name',
            'status_label', 'created_at',
        ]
        
        
class ComplaintDetailSerializer(serializers.ModelSerializer):
    booking_code = serializers.CharField(source='booking.booking_code', read_only=True)
    schedule_sequence_no = serializers.IntegerField(
        source='schedule.sequence_no', read_only=True, default=None,
    )
    attachments = ComplaintAttachmentSerializer(many=True, read_only=True)
    reporter_name = serializers.CharField(source='reporter.get_full_name', read_only=True)
    worker_name = serializers.CharField(source='worker.get_full_name', read_only=True, default=None)
    resolved_by_name = serializers.CharField(
        source='resolved_by.get_full_name', read_only=True, default=None,
    )
    issue_type_name = serializers.CharField(source='issue_type.name', read_only=True)
    issue_type_code = serializers.CharField(source='issue_type.code', read_only=True)
    stage_label = serializers.CharField(source='get_stage_display', read_only=True)
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    my_amount = serializers.SerializerMethodField()
    booking_is_cash = serializers.SerializerMethodField()

    class Meta:
        model = Complaint
        fields = [
            'id', 'code', 'reporter', 'reporter_role', 'reporter_name',
            'worker', 'worker_name',
            'booking', 'booking_code', 'booking_is_cash', 'schedule', 'schedule_sequence_no',
            'issue_type', 'issue_type_code', 'issue_type_name',
            'stage', 'stage_label',
            'content',
            'status', 'status_label',
            'resolved_by', 'resolved_by_name', 'resolution_note', 'resolved_at',
            'outcome', 'refund_amount', 'my_amount',
            'customer_delta', 'worker_delta', 'shortfall',
            'created_at', 'attachments',
        ]
        read_only_fields = fields

    def get_booking_is_cash(self, obj):
        return any(p.method == 'CASH' for p in obj.booking.payments.all())

    def get_my_amount(self, obj):
        """Số tiền đã cộng (+) / trừ (-) vào ví của CHÍNH người gửi khiếu nại."""
        request = self.context.get('request')
        if not request or obj.reporter_id != request.user.id:
            return None
        delta = obj.worker_delta if obj.reporter_role == Complaint.ReporterRole.WORKER else obj.customer_delta
        return str(delta)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        if not (request and get_user_role(request.user) == 'ADMIN'):
            for key in ('customer_delta', 'worker_delta', 'shortfall'):
                data.pop(key, None)
        return data
    
    
    
    
class ComplaintResolveSerializer(serializers.ModelSerializer):
    status = serializers.ChoiceField(choices=[
        Complaint.Status.IN_REVIEW.value,
        Complaint.Status.RESOLVED.value,
        Complaint.Status.REJECTED.value,
    ])
    outcome = serializers.ChoiceField(
        choices=[Complaint.Outcome.REFUND_CUSTOMER.value, Complaint.Outcome.PAY_WORKER.value],
        required=False,
    )
    # Chỉ dùng với REFUND_CUSTOMER: True = hủy/thu hồi thu nhập buổi đó của nhân viên.
    charge_worker = serializers.BooleanField(default=True)

    class Meta:
        model = Complaint
        fields = ['status', 'resolution_note', 'outcome', 'charge_worker']

    def validate(self, attrs):
        c = self.instance
        if c.status in (
            Complaint.Status.RESOLVED, Complaint.Status.REJECTED, Complaint.Status.CANCELLED,
        ):
            raise serializers.ValidationError('Khiếu nại đã đóng, không thể xử lý lại.')

        outcome = attrs.get('outcome')
        if outcome:
            if attrs['status'] != Complaint.Status.RESOLVED:
                raise serializers.ValidationError({'outcome': 'Chỉ xử lý tiền khi trạng thái là RESOLVED.'})
            if outcome == Complaint.Outcome.REFUND_CUSTOMER and c.reporter_role != Complaint.ReporterRole.CUSTOMER:
                raise serializers.ValidationError({'outcome': 'Chỉ hoàn tiền khách cho khiếu nại của khách.'})
            if outcome == Complaint.Outcome.PAY_WORKER and c.reporter_role != Complaint.ReporterRole.WORKER:
                raise serializers.ValidationError({'outcome': 'Chỉ trả tiền nhân viên cho khiếu nại của nhân viên.'})
        return attrs

    def update(self, instance, validated_data):
        request = self.context['request']
        instance.status = validated_data['status']
        instance.resolution_note = validated_data.get('resolution_note', instance.resolution_note)

        if instance.status in (Complaint.Status.RESOLVED, Complaint.Status.REJECTED):
            instance.resolved_by = request.user
            instance.resolved_at = timezone.now()
        else:
            instance.resolved_by = None
            instance.resolved_at = None

        outcome = validated_data.get('outcome')
        if instance.status == Complaint.Status.RESOLVED and outcome:
            from . import resolution_service
            resolution_service.apply_outcome(
                complaint=instance,
                admin=request.user,
                outcome=outcome,
                charge_worker=validated_data.get('charge_worker', True),
            )

        instance.save()
        return instance


class ComplaintCancelSerializer(serializers.Serializer):
    def save(self):
        complaint = self.context['complaint']
        request = self.context.get('request')

        if request and complaint.reporter_id != request.user.id:
            raise serializers.ValidationError('Bạn không thể hủy khiếu nại của người khác.')
        if complaint.status != Complaint.Status.PENDING:
            raise serializers.ValidationError('Chỉ có thể hủy khiếu nại đang chờ xử lý.')

        complaint.status = Complaint.Status.CANCELLED
        complaint.save(update_fields=['status'])
        return complaint