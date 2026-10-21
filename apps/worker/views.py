from django.db.models import Prefetch, Q
from django.utils.dateparse import parse_date
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.bookings.models import BookingSchedule
from apps.common.permissions import IsAdminRole, IsCustomerRole, IsWorkerRole

from . import assignment_service, checkin_service, favorite_worker_service
from .models import Area, BookingAssignment, WorkerWorkingArea

from django.core.cache import cache
from apps.common.cache_utils import versioned_key

from apps.common.idempotency import idempotent
from rest_framework.throttling import ScopedRateThrottle
from apps.common.distributed_lock import distributed_lock

from .serializers import (
    AdminAssignWorkerSerializer,
    AreaSummarySerializer,
    BookingScheduleImageSerializer,
    CancelAssignmentSerializer,
    ClaimBookingPackageSerializer,
    CustomerWorkerProfileSerializer,
    FavoriteWorkerSerializer,
    ScheduleImageUploadSerializer,
    WorkerBookingScheduleSerializer,
    WorkerMyScheduleSerializer,
    WorkerScheduleSerializer,
    WorkerWorkingAreaBulkUpdateSerializer,
    WorkerWorkingAreaSerializer,
    CheckOutSerializer,
    CheckInSerializer
)

from .schemas import (
    WORKER_ACTIVE_AREA_SCHEMA,
    WORKER_WORKING_AREA_SCHEMA,
    WORKER_AVAILABLE_SCHEDULE_SCHEMA,
    WORKER_MY_SCHEDULE_SCHEMA,
    WORKER_CLAIM_SCHEDULE_SCHEMA,
    WORKER_CLAIM_BOOKING_PACKAGE_SCHEMA,
    WORKER_CANCEL_ASSIGNMENT_SCHEMA,
    ADMIN_ASSIGN_WORKER_SCHEMA,
    CUSTOMER_FAVORITE_WORKER_DETAIL_SCHEMA,
    CUSTOMER_FAVORITE_WORKER_LIST_SCHEMA,
    CUSTOMER_WORKER_PROFILE_SCHEMA,
)


class FavoriteWorkerPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 50


@CUSTOMER_WORKER_PROFILE_SCHEMA
class CustomerWorkerProfileView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    serializer_class = CustomerWorkerProfileSerializer

    def get(self, request, *args, **kwargs):
        worker = favorite_worker_service.get_worker_for_customer(
            customer=request.user,
            worker_id=kwargs['worker_id'],
        )
        return Response({
            'message': 'Lấy hồ sơ nhân viên thành công.',
            'data': self.get_serializer(worker).data,
        })


def _int_param(request, name):
    raw = request.query_params.get(name)
    if not raw:
        return None
    if not raw.isdigit():
        raise ValidationError({name: f'{name} phải là số nguyên.'})
    return int(raw)



@CUSTOMER_FAVORITE_WORKER_LIST_SCHEMA
class CustomerFavoriteWorkerListView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    serializer_class = FavoriteWorkerSerializer
    pagination_class = FavoriteWorkerPagination

    def get(self, request, *args, **kwargs):
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(
            favorite_worker_service.list_favorite_workers(
                customer=request.user,
                service_id=_int_param(request, 'service_id'),
                address_id=_int_param(request, 'address_id'),
            ),
            request,
            view=self,
        )
        return Response({
            'message': 'Lấy danh sách nhân viên yêu thích thành công.',
            'data': {
                'results': self.get_serializer(page, many=True).data,
                'count': paginator.page.paginator.count,
                'page': paginator.page.number,
                'total_pages': paginator.page.paginator.num_pages,
                'has_next': paginator.page.has_next(),
                'has_previous': paginator.page.has_previous(),
                'page_size': paginator.get_page_size(request),
            },
        })


@CUSTOMER_FAVORITE_WORKER_DETAIL_SCHEMA
class CustomerFavoriteWorkerDetailView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    serializer_class = FavoriteWorkerSerializer

    def put(self, request, *args, **kwargs):
        favorite, created = favorite_worker_service.add_favorite_worker(
            customer=request.user,
            worker_id=kwargs['worker_id'],
        )
        return Response({
            'message': (
                'Thêm nhân viên yêu thích thành công.'
                if created
                else 'Nhân viên đã có trong danh sách yêu thích.'
            ),
            'data': self.get_serializer(favorite).data,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def delete(self, request, *args, **kwargs):
        favorite_worker_service.remove_favorite_worker(
            customer=request.user,
            worker_id=kwargs['worker_id'],
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

# select_related dùng chung: customer_avatar/customer_name/payment_status/
# service_data/form_schema đều đọc qua booking__customer và booking__service,
# nếu không select_related trước sẽ bị N+1 query (1 query/booking) khi
# serializer truy cập instance.booking.customer, instance.booking.service.
def _base_select_related(queryset):
    return queryset.select_related(
        'booking', 'booking__customer', 'booking__service', 'booking__address',
    )


def _prefetch_assignments(queryset, *, with_images=False):
    queryset = _base_select_related(queryset).prefetch_related(
        Prefetch('assignments', queryset=BookingAssignment.objects.select_related('assigned_by', 'worker')),
        'booking__payments',
    )
    if with_images:
        queryset = queryset.prefetch_related('images')
    return queryset


def _parse_date_param(request, name):
    raw = request.query_params.get(name)
    if not raw:
        return None
    parsed = parse_date(raw)
    if parsed is None:
        raise ValidationError({name: 'Định dạng ngày phải là YYYY-MM-DD.'})
    return parsed


def _parse_booking_id_param(request):
    raw = request.query_params.get('booking_id')
    if not raw:
        return None
    if not raw.isdigit():
        raise ValidationError({'booking_id': 'booking_id phải là số nguyên.'})
    return int(raw)


class WorkerSchedulePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 50


def _paginate_or_full(request, view, queryset, serializer_class, booking_id, message):
    """
    booking_id có giá trị -> đang xem chi tiết 1 gói (màn chọn buổi để
    claim), FE cần thấy TOÀN BỘ buổi cùng lúc để tick chọn, không phân
    trang. Không có booking_id -> danh sách chính, phân trang để tránh
    tải hết mọi buổi (gói tháng có thể 20-30 buổi) trong 1 lần.
    """
    if booking_id:
        serializer = serializer_class(queryset, many=True, context={'request': request})
        return Response({'message': message, 'data': serializer.data})

    paginator = WorkerSchedulePagination()
    page = paginator.paginate_queryset(queryset, request, view=view)
    serializer = serializer_class(page, many=True, context={'request': request})
    return Response({
        'message': message,
        'data': {
            'results': serializer.data,
            'count': paginator.page.paginator.count,
            'page': paginator.page.number,
            'total_pages': paginator.page.paginator.num_pages,
            'has_next': paginator.page.has_next(),
            'has_previous': paginator.page.has_previous(),
            'page_size': paginator.get_page_size(request),
        },
    })


@WORKER_ACTIVE_AREA_SCHEMA
class WorkerActiveAreaListView(generics.ListAPIView):
    permission_classes = [IsWorkerRole | IsCustomerRole] 
    serializer_class = AreaSummarySerializer

    def get_queryset(self):
        queryset = Area.objects.filter(is_active=True)
        province_code = (self.request.query_params.get('province_code') or '').strip()
        search = (self.request.query_params.get('search') or '').strip()
        if province_code:
            queryset = queryset.filter(province_code=province_code)
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset

    def list(self, request, *args, **kwargs):
        province_code = (request.query_params.get('province_code') or '').strip()
        search = (request.query_params.get('search') or '').strip()
        if not province_code and not search:
            raise ValidationError({'province_code': 'Vui lòng chọn tỉnh/thành.'})

        cache_key = None if search else versioned_key('areas', 'list', province_code)
        data = cache.get(cache_key) if cache_key else None
        if data is None:
            data = [*self.get_serializer(self.get_queryset(), many=True).data]
            if cache_key:
                cache.set(cache_key, data, 60 * 60)
        return Response(data)


class WorkerActiveProvinceListView(generics.GenericAPIView):
    """Danh sách 34 tỉnh/thành để chọn trước, rồi mới chọn phường/xã."""
    permission_classes = [IsWorkerRole | IsCustomerRole]

    def get(self, request, *args, **kwargs):
        data = cache.get('areas:provinces')
        if data is None:
            data = list(
                Area.objects.filter(is_active=True)
                .values('province_code', 'city')
                .distinct()
                .order_by('city')
            )
            cache.set('areas:provinces', data, 60 * 60)
        return Response(data)


@WORKER_WORKING_AREA_SCHEMA
class WorkerWorkingAreaView(generics.GenericAPIView):
    permission_classes = [IsWorkerRole]

    def get_queryset(self):
        return (
            WorkerWorkingArea.objects.filter(worker=self.request.user)
            .select_related('area')
            .order_by('area__city', 'area__name')
        )

    def get(self, request, *args, **kwargs):
        serializer = WorkerWorkingAreaSerializer(self.get_queryset(), many=True)
        return Response({'message': 'Lấy danh sách khu vực làm việc thành công.', 'data': serializer.data})

    def put(self, request, *args, **kwargs):
        serializer = WorkerWorkingAreaBulkUpdateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        queryset = serializer.save()
        return Response({
            'message': 'Cập nhật khu vực làm việc thành công.',
            'data': WorkerWorkingAreaSerializer(queryset, many=True).data,
        })


@WORKER_AVAILABLE_SCHEDULE_SCHEMA
class WorkerAvailableScheduleListView(generics.GenericAPIView):
    permission_classes = [IsWorkerRole]
    serializer_class = WorkerScheduleSerializer

    def get(self, request, *args, **kwargs):
        booking_id = _parse_booking_id_param(request)
        queryset = _prefetch_assignments(assignment_service.list_available_schedules_for_worker(
            request.user,
            booking_id=booking_id,
            date_from=_parse_date_param(request, 'date_from'),
            date_to=_parse_date_param(request, 'date_to'),
            group_by_booking=request.query_params.get('group_by') == 'booking',
        ))
        return _paginate_or_full(
            request, self, queryset, self.get_serializer_class(), booking_id,
            'Lấy danh sách buổi làm việc khả dụng thành công.',
        )

@WORKER_MY_SCHEDULE_SCHEMA
class WorkerMyScheduleListView(generics.GenericAPIView):
    permission_classes = [IsWorkerRole]
    serializer_class = WorkerMyScheduleSerializer

    def get(self, request, *args, **kwargs):
        schedule_status = request.query_params.get('status')
        if schedule_status:
            schedule_status = schedule_status.upper()
            if schedule_status not in BookingSchedule.Status.values:
                raise ValidationError({'status': 'Trạng thái buổi làm không hợp lệ.'})

        booking_id = _parse_booking_id_param(request)

        tab = request.query_params.get('tab')
        if tab and tab not in assignment_service.MY_TABS:
            raise ValidationError({'tab': 'Tab không hợp lệ.'})

        queryset = _prefetch_assignments(
            assignment_service.list_my_schedules(
                request.user, schedule_status=schedule_status,
                booking_id=booking_id, tab=tab,
            ),
            with_images=True,
        )
        return _paginate_or_full(
            request, self, queryset, self.get_serializer_class(), booking_id,
            'Lấy danh sách buổi làm việc của tôi thành công.',
        )



class WorkerBookingScheduleListView(generics.GenericAPIView):
    """Toàn bộ buổi của 1 gói, kèm claim_state — cho màn chi tiết gói."""
    permission_classes = [IsWorkerRole]
    serializer_class = WorkerBookingScheduleSerializer

    def get(self, request, *args, **kwargs):
        queryset = _prefetch_assignments(
            assignment_service.list_booking_schedules_for_worker(
                request.user, booking_id=kwargs['booking_id'],
            ),
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'message': 'Lấy danh sách buổi làm việc của gói thành công.',
            'data': serializer.data,
        })


class WorkerClaimScheduleView(APIView):
    permission_classes = [IsWorkerRole]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'worker_action'

    @idempotent
    def post(self, request, *args, **kwargs):
        import logging, time
        logger = logging.getLogger(__name__)
        t0 = time.monotonic()

        with distributed_lock(f"schedule:{kwargs['schedule_id']}", ttl=30):
            assignment = assignment_service.claim_schedule(
                schedule_id=kwargs['schedule_id'], worker=request.user,
            )
        t1 = time.monotonic()

        # Tạo chat SAU khi claim đã commit và nhả lock; lỗi chat không làm hỏng việc nhận.
        from apps.chat.service import ensure_chat_for_assignment
        try:
            ensure_chat_for_assignment(assignment)
        except Exception:
            pass

        logger.warning('claim: db=%.2fs chat=%.2fs', t1 - t0, time.monotonic() - t1)
        return Response({
            'message': 'Nhận việc thành công.',
            'data': {
                'assignment_id': assignment.id,
                'schedule_id': assignment.schedule_id,
                'status': assignment.status,
            },
        }, status=201)


@WORKER_CLAIM_BOOKING_PACKAGE_SCHEMA
class WorkerClaimBookingPackageView(generics.GenericAPIView):
    """
    Nhận buổi trong 1 booking (đơn định kỳ nhiều buổi).

    Body rỗng hoặc không có `schedule_ids` -> nhận TOÀN BỘ buổi PENDING
    còn trống của đơn (hành vi cũ).
    Body có `schedule_ids: [id, ...]` -> chỉ nhận đúng các buổi đó, cho
    phép nhân viên nhận 1 buổi hoặc 1 phần buổi trong gói.

    Buổi nào trùng khung giờ với buổi khác của chính nhân viên (kể cả
    buổi vừa nhận trong cùng request) sẽ bị bỏ qua (skipped) kèm lý do,
    không làm fail toàn bộ request — FE dùng đó để báo cho nhân viên biết
    vì sao không nhận được buổi đó.
    """
    permission_classes = [IsWorkerRole]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'worker_action'
    serializer_class = ClaimBookingPackageSerializer

    @idempotent
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        schedule_ids = serializer.validated_data.get('schedule_ids')  # None = nhận toàn bộ

        with distributed_lock(f"booking:{kwargs['booking_id']}"):
            result = assignment_service.claim_booking_package(
                booking_id=kwargs['booking_id'], worker=request.user, schedule_ids=schedule_ids,
            )
        claimed, skipped = result['claimed'], result['skipped']

        # ĐỔI: tạo chat SAU KHI transaction claim đã commit xong (nằm
        # ngoài assignment_service.claim_booking_package), để phần ghi DB
        # cốt lõi trả lời nhanh nhất có thể — giảm khả năng client bị
        # ERR_NETWORK giữa lúc server vẫn đang xử lý. Lỗi tạo chat (nếu
        # có) không được để làm hỏng response nhận việc, vì buổi đã claim
        # thành công rồi, không nên rollback hay báo lỗi oan cho worker.
        from apps.chat.service import ensure_chat_for_assignment
        for assignment in claimed:
            try:
                ensure_chat_for_assignment(assignment)
            except Exception:
                pass

        message = f'Đã nhận {len(claimed)} buổi.'
        if skipped:
            message += f' Bỏ qua {len(skipped)} buổi (đã có người nhận, trùng lịch, hoặc không hợp lệ).'
        return Response({
            'message': message,
            'data': {
                'claimed': [
                    {'assignment_id': a.id, 'schedule_id': a.schedule_id}
                    for a in claimed
                ],
                'skipped': skipped,
            },
        }, status=201)


class WorkerDeclinePreferredView(APIView):
    permission_classes = [IsWorkerRole]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'worker_action'

    @idempotent
    def post(self, request, *args, **kwargs):
        with distributed_lock(f"booking:{kwargs['booking_id']}"):
            assignment_service.decline_preferred_request(
                booking_id=kwargs['booking_id'], worker=request.user,
            )
        return Response({'message': 'Đã từ chối. Đơn được mở cho nhân viên khác.'})

@WORKER_CANCEL_ASSIGNMENT_SCHEMA
class WorkerCancelAssignmentView(generics.GenericAPIView):
    permission_classes = [IsWorkerRole]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'worker_action'
    serializer_class = CancelAssignmentSerializer

    @idempotent
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment = assignment_service.cancel_assignment(
            assignment_id=kwargs['assignment_id'], worker=request.user,
            reason=serializer.validated_data['reason'],
        )
        return Response({
            'message': 'Hủy nhận việc thành công.',
            'data': {
                'assignment_id': assignment.id,
                'schedule_id': assignment.schedule_id,
                'status': assignment.status,
            },
        })


@ADMIN_ASSIGN_WORKER_SCHEMA
class AdminAssignWorkerView(generics.GenericAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = AdminAssignWorkerSerializer

    @idempotent
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with distributed_lock(f"schedule:{kwargs['schedule_id']}"):
            assignment = assignment_service.admin_assign_worker(
                schedule_id=kwargs['schedule_id'],
                worker_id=serializer.validated_data['worker_id'],
                admin_user=request.user,
                response_minutes=serializer.validated_data['response_minutes'],
            )
        return Response({'message': 'Đã gửi lời mời nhận việc.', 'data': {'assignment_id': assignment.id}}, status=201)


class WorkerCheckInView(generics.GenericAPIView):
    permission_classes = [IsWorkerRole]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'worker_action'
    serializer_class = CheckInSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        schedule = checkin_service.check_in(
            schedule_id=kwargs['schedule_id'],
            worker=request.user,
            latitude=serializer.validated_data['latitude'],
            longitude=serializer.validated_data['longitude'],
            accuracy=serializer.validated_data.get('accuracy'),
        )
        return Response({
            'message': 'Check-in thành công.',
            'data': WorkerMyScheduleSerializer(schedule).data,
        })


class WorkerCheckOutView(generics.GenericAPIView):
    permission_classes = [IsWorkerRole]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'worker_action'
    serializer_class = CheckOutSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        schedule = checkin_service.check_out(
            schedule_id=kwargs['schedule_id'],
            worker=request.user,
            completion_note=serializer.validated_data.get('completion_note'),  # THÊM
        )
        return Response({
            'message': 'Check-out thành công.',
            'data': WorkerMyScheduleSerializer(schedule).data,
        })


class WorkerScheduleImageUploadView(generics.GenericAPIView):
    permission_classes = [IsWorkerRole]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'upload'
    parser_classes = [MultiPartParser, FormParser]
    serializer_class = ScheduleImageUploadSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        image = checkin_service.upload_schedule_image(
            schedule_id=kwargs['schedule_id'],
            worker=request.user,
            file=serializer.validated_data['image'],
            image_type=serializer.validated_data['image_type'],
            note=serializer.validated_data.get('note'),
        )
        return Response({
            'message': 'Tải ảnh thành công.',
            'data': BookingScheduleImageSerializer(image).data,
        })


class WorkerInvitationResponseView(generics.GenericAPIView):
    permission_classes = [IsWorkerRole]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'worker_action'

    @idempotent
    def post(self, request, invitation_id):
        from .invitation_service import respond_invitation
        action = request.data.get('action')
        if action not in ('accept', 'decline'):
            raise ValidationError({'action': 'Chọn accept hoặc decline.'})
        invitation = respond_invitation(invitation_id=invitation_id, worker=request.user, accept=action == 'accept')
        if action == 'accept':
            from apps.chat.service import ensure_chat_for_assignment
            try:
                ensure_chat_for_assignment(invitation)
            except Exception:
                pass
        return Response({'message': 'Đã nhận việc.' if action == 'accept' else 'Đã từ chối lời mời.',
                         'data': {'id': invitation.id, 'status': invitation.status}})
        
        

class WorkerConfirmCashView(APIView):
    permission_classes = [IsWorkerRole]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'worker_action'

    def post(self, request, *args, **kwargs):
        payment = checkin_service.confirm_cash_collected(
            schedule_id=kwargs['schedule_id'], worker=request.user,
        )
        return Response({
            'message': 'Đã xác nhận thu tiền mặt.',
            'data': {'payment_id': payment.id, 'status': payment.status},
        })
        
        
class WorkerMyScheduleCountsView(APIView):
    permission_classes = [IsWorkerRole]

    def get(self, request, *args, **kwargs):
        return Response({
            'message': 'Lấy số lượng đơn theo tab thành công.',
            'data': assignment_service.count_my_schedules_by_tab(request.user),
        })
