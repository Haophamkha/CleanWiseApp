from django.db.models import Prefetch
from django.shortcuts import get_object_or_404

from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.common.permissions import IsCustomerRole
from apps.worker.models import BookingAssignment

from apps.common.idempotency import idempotent
from apps.worker.assignment_service import run_lazy_expiry

from rest_framework.throttling import ScopedRateThrottle
from apps.common.distributed_lock import distributed_lock

from .schemas import (
    BOOKING_CUSTOMER_SCHEMA,
    BOOKING_DETAIL_CUSTOMER_SCHEMA,
)
from .models import Booking
from .serializers import (
    BookingCreateSerializer,
    BookingDetailSerializer,
    BookingListSerializer,
)

from django.conf import settings

from apps.payments.models import Payment
from apps.payments.payment_link_service import create_payos_payment_link


class BookingPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 50


# ĐỔI: gom logic prefetch schedules__assignments ra hàm dùng chung,
# tránh lặp lại giữa post() và BookingDetailView.get_object() —
# cả 2 chỗ đều cần prefetch giống hệt nhau trước khi đưa qua
# BookingDetailSerializer (serializer này gọi obj.assignments.all()
# 3 lần/schedule qua BookingScheduleSerializer, nếu không prefetch
# sẽ tạo N+1 query rất chậm khi booking có nhiều buổi, vd. dịch vụ
# định kỳ 13+ buổi -> 39+ query round-trip tới DB, dễ vượt timeout FE).
def _accepted_assignments_queryset():
    return (
        BookingAssignment.objects
        .filter(
            status=BookingAssignment.Status.ACCEPTED,
        )
        .select_related(
            'worker',
            'worker__worker_profile',
            'chat_link',
        )
    )


@BOOKING_CUSTOMER_SCHEMA
class BookingListCreateView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    pagination_class = BookingPagination

    def get_throttles(self):
        throttles = super().get_throttles()
        if self.request.method == 'POST':
            self.throttle_scope = 'booking'
            throttles.append(ScopedRateThrottle())
        return throttles
    
    def get_queryset(self):
        run_lazy_expiry()

        queryset = (
            Booking.objects
            .filter(
                customer=self.request.user,
            )
            .select_related('service')
            .order_by('-created_at')
        )

        status_param = self.request.query_params.get('status')

        if status_param:
            status_param = status_param.upper()

            if status_param not in Booking.Status.values:
                raise ValidationError({
                    'status': 'Trạng thái đơn dịch vụ không hợp lệ.',
                })

            queryset = queryset.filter(
                status=status_param,
            )

        return queryset

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return BookingCreateSerializer

        return BookingListSerializer

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()

        paginator = self.pagination_class()

        page = paginator.paginate_queryset(
            queryset,
            request,
            view=self,
        )

        serialized = BookingListSerializer(
            page,
            many=True,
        ).data

        return Response({
            'message': 'Lấy danh sách đơn dịch vụ thành công.',
            'data': {
                'results': serialized,
                'count': paginator.page.paginator.count,
                'page': paginator.page.number,
                'total_pages': paginator.page.paginator.num_pages,
                'has_next': paginator.page.has_next(),
                'has_previous': paginator.page.has_previous(),
                'page_size': paginator.get_page_size(request),
            },
        })

    @idempotent
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(
            data=request.data,
        )

        serializer.is_valid(
            raise_exception=True,
        )

        booking = serializer.save()

        booking = (
            Booking.objects
            .select_related('service', 'address', 'delivery_address')
            .prefetch_related(
                Prefetch(
                    'schedules__assignments',
                    queryset=_accepted_assignments_queryset(),
                ),
                'schedules__images',
            )
            .get(pk=booking.pk)
        )

        return Response(
            {
                'message': 'Đặt dịch vụ thành công.',
                'data': BookingDetailSerializer(
                    booking,
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )


@BOOKING_DETAIL_CUSTOMER_SCHEMA
class BookingDetailView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    serializer_class = BookingDetailSerializer

    def get_object(self):
        run_lazy_expiry()

        return get_object_or_404(
            Booking.objects
            .filter(
                pk=self.kwargs['pk'],
                customer=self.request.user,
            )
            .select_related('service')
            .prefetch_related(
                Prefetch(
                    'schedules__assignments',
                    queryset=_accepted_assignments_queryset(),
                ),
                'schedules__images',
            ),
        )

    def get(self, request, *args, **kwargs):
        booking = self.get_object()

        serializer = self.get_serializer(
            booking,
        )

        return Response({
            'message': 'Lấy chi tiết đơn dịch vụ thành công.',
            'data': serializer.data,
        })


from .booking_service import cancel_booking, cancel_schedule_by_customer
from .serializers import BookingCancelSerializer


class BookingCancelView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    serializer_class = BookingCancelSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'booking'

    @idempotent
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with distributed_lock(f"booking:{kwargs['pk']}"):
            booking = cancel_booking(
                booking_id=kwargs['pk'],
                customer=request.user,
                reason=serializer.validated_data['reason'],
            )
        return Response({
            'message': 'Hủy đơn thành công.',
            'data': {
                'id': booking.id,
                'status': booking.status,
                'payment_status': booking.payment_status,
                'refunded_amount': str(booking.refunded_amount),
            },
        })

    

class BookingPaymentLinkView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payment'

    @idempotent
    def post(self, request, *args, **kwargs):
        payment = get_object_or_404(
            Payment.objects.select_related('booking'),
            booking_id=kwargs['pk'],
            booking__customer=request.user,
            booking__status=Booking.Status.PENDING,
            booking__payment_status=Booking.PaymentStatus.UNPAID,
            method=Payment.Method.BANK_TRANSFER,
            status=Payment.Status.PENDING,
        )

        link = create_payos_payment_link(
            payment,
            return_url=f'{settings.PAYOS_RETURN_URL}?bookingId={payment.booking_id}',
            cancel_url=f'{settings.PAYOS_CANCEL_URL}?bookingId={payment.booking_id}',
        )

        return Response({
            'message': 'Tạo mã QR thanh toán thành công.',
            'data': link,
        })
        
        
class ScheduleCancelView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    serializer_class = BookingCancelSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'booking'

    @idempotent
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with distributed_lock(f"schedule:{kwargs['pk']}"):
            schedule = cancel_schedule_by_customer(
                schedule_id=kwargs['pk'],
                customer=request.user,
                reason=serializer.validated_data['reason'],
            )
        booking = Booking.objects.get(pk=schedule.booking_id)
        return Response({
            'message': 'Hủy buổi thành công.',
            'data': {
                'schedule_id': schedule.id,
                'schedule_status': schedule.status,
                'booking_status': booking.status,
                'payment_status': booking.payment_status,
                'refunded_amount': str(booking.refunded_amount),
            },
        })