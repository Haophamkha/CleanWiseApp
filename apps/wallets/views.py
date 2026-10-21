from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from apps.common.idempotency import idempotent
from apps.common.permissions import IsCustomerRole, IsWorkerRole

from . import topup_service, wallet_service, withdraw_service,withdraw_otp
from .models import WalletTopup, WalletTransaction, WithdrawRequest
from .serializers import (
    TopupRequestSerializer,
    WalletSerializer,
    WalletTopupSerializer,
    WalletTransactionSerializer,
    WithdrawRecordSerializer,
    WithdrawRequestSerializer,
)


class WalletTransactionPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 50


WITHDRAW_MESSAGES = {
    WithdrawRequest.Status.SUCCESS: 'Rút tiền thành công.',
    WithdrawRequest.Status.PROCESSING: 'Yêu cầu rút tiền đã được gửi, tiền sẽ về tài khoản trong ít phút.',
    WithdrawRequest.Status.FAILED: 'Rút tiền thất bại, số tiền đã được hoàn lại vào ví.',
}


def _withdraw_response(withdraw):
    return Response({
        'message': WITHDRAW_MESSAGES[withdraw.status],
        'data': WithdrawRecordSerializer(withdraw).data,
    })


# ---------------------------------------------------------------- base dùng chung

class _WalletDetailBase(generics.GenericAPIView):
    serializer_class = WalletSerializer

    def get(self, request, *args, **kwargs):
        wallet, _ = wallet_service.get_or_create_wallet(request.user)
        return Response({
            'message': 'Lấy thông tin ví thành công.',
            'data': self.get_serializer(wallet).data,
        })


class _WalletTransactionListBase(generics.GenericAPIView):
    serializer_class = WalletTransactionSerializer
    pagination_class = WalletTransactionPagination

    def get_queryset(self):
        return WalletTransaction.objects.filter(
            wallet__user=self.request.user,
        ).select_related('booking').order_by('-created_at', '-id')

    def get(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serialized = self.get_serializer(page, many=True).data
        return Response({
            'message': 'Lấy lịch sử giao dịch thành công.',
            'data': {
                'results': serialized,
                'count': paginator.page.paginator.count,
                'page': paginator.page.number,
                'total_pages': paginator.page.paginator.num_pages,
                'has_next': paginator.page.has_next(),
                'has_previous': paginator.page.has_previous(),
            },
        })


class _WithdrawCreateBase(generics.GenericAPIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payment'
    serializer_class = WithdrawRequestSerializer

    @idempotent(required=True)
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        withdraw = withdraw_service.create_withdraw(
            user=request.user,
            amount=serializer.validated_data['amount'],
            payment_method_id=serializer.validated_data.get('payment_method_id'),
            otp=request.data.get('otp'),
        )
        return _withdraw_response(withdraw)


class _TopupCreateBase(generics.GenericAPIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payment'
    serializer_class = TopupRequestSerializer

    @idempotent(required=True)
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        topup = topup_service.create_topup(user=request.user, amount=serializer.validated_data['amount'])
        return Response({
            'message': 'Đã tạo liên kết nạp tiền.',
            'data': WalletTopupSerializer(topup).data,
        })


class _TopupDetailBase(generics.GenericAPIView):
    serializer_class = WalletTopupSerializer

    def get(self, request, pk, *args, **kwargs):
        topup = get_object_or_404(WalletTopup, pk=pk, user=request.user)
        topup = topup_service.refresh_topup(topup)  # lệnh còn PENDING thì hỏi lại payOS (phòng mất webhook)
        return Response({'message': 'Lấy trạng thái nạp tiền thành công.', 'data': self.get_serializer(topup).data})


class _WithdrawDetailBase(generics.GenericAPIView):
    serializer_class = WithdrawRecordSerializer

    def get(self, request, pk, *args, **kwargs):
        withdraw = get_object_or_404(WithdrawRequest, pk=pk, user=request.user)
        withdraw = withdraw_service.refresh_withdraw(withdraw)  # lệnh đang xử lý thì hỏi lại payOS (có throttle)
        return Response({'message': 'Lấy trạng thái rút tiền thành công.', 'data': self.get_serializer(withdraw).data})


# ---------------------------------------------------------------- customer

class WalletDetailView(_WalletDetailBase):
    """GET số dư ví của khách hàng hiện tại."""
    permission_classes = [IsCustomerRole]


class WalletTransactionListView(_WalletTransactionListBase):
    """GET lịch sử giao dịch ví, phân trang."""
    permission_classes = [IsCustomerRole]


class WalletWithdrawRequestView(_WithdrawCreateBase):
    """POST rút tiền — tự động chi về ngân hàng qua payOS, không cần admin duyệt. Bắt buộc header Idempotency-Key."""
    permission_classes = [IsCustomerRole]


class WalletTopupCreateView(_TopupCreateBase):
    """POST tạo link nạp tiền vào ví khách. Bắt buộc header Idempotency-Key."""
    permission_classes = [IsCustomerRole]


class WalletTopupDetailView(_TopupDetailBase):
    permission_classes = [IsCustomerRole]


class WalletWithdrawDetailView(_WithdrawDetailBase):
    permission_classes = [IsCustomerRole]
    
    
class _WithdrawOtpBase(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp'

    def post(self, request, *args, **kwargs):
        withdraw_otp.send_withdraw_otp(request.user)
        return Response({'message': 'Đã gửi mã OTP đến email của bạn.'})


class WalletWithdrawOtpView(_WithdrawOtpBase):
    permission_classes = [IsCustomerRole]


class WorkerWalletWithdrawOtpView(_WithdrawOtpBase):
    permission_classes = [IsWorkerRole]


# ---------------------------------------------------------------- worker

class WorkerWalletDetailView(_WalletDetailBase):
    """GET số dư ví ký quỹ của nhân viên hiện tại."""
    permission_classes = [IsWorkerRole]


class WorkerWalletTransactionListView(_WalletTransactionListBase):
    """GET lịch sử giao dịch ví ký quỹ, phân trang."""
    permission_classes = [IsWorkerRole]


class WorkerWalletWithdrawRequestView(_WithdrawCreateBase):
    """POST rút tiền — phải chừa lại tối thiểu 400.000đ ký quỹ, tự động chi qua payOS. Bắt buộc Idempotency-Key."""
    permission_classes = [IsWorkerRole]


class WorkerWalletTopupCreateView(_TopupCreateBase):
    """POST tạo link nạp tiền vào ví ký quỹ nhân viên. Bắt buộc Idempotency-Key."""
    permission_classes = [IsWorkerRole]


class WorkerWalletTopupDetailView(_TopupDetailBase):
    permission_classes = [IsWorkerRole]


class WorkerWalletWithdrawDetailView(_WithdrawDetailBase):
    permission_classes = [IsWorkerRole]


# ---------------------------------------------------------------- mock (chỉ route khi DEBUG / PAYOUT_ALLOW_MOCK)

class MockTopupConfirmView(generics.GenericAPIView):
    """POST giả lập "đã thanh toán" để test khi chưa có payOS. Chỉ hoạt động với TOPUP_MODE=mock."""
    permission_classes = [IsAuthenticated]
    serializer_class = WalletTopupSerializer

    def post(self, request, pk, *args, **kwargs):
        topup = topup_service.mock_confirm_topup(topup_id=pk, user=request.user)
        return Response({'message': 'Đã giả lập thanh toán thành công.', 'data': self.get_serializer(topup).data})