import logging
import re
import uuid
from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.core.exceptions import ObjectDoesNotExist
from payos import WebhookError
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError
from apps.common.permissions import IsCustomerRole, IsWorkerRole
from apps.common.encryption import encrypt_value, decrypt_value

from . import history_service, webhook_service
from . import bankhub
from .bank_catalog import get_bank_catalog
from .models import BankAccountVerification, UserPaymentMethod
from .payment_method_service import set_default_payment_method, soft_delete_payment_method
from .serializers import (
    BankPaymentMethodCreateSerializer,
    UserPaymentMethodSerializer,
    UserPaymentMethodUpdateSerializer,
)

logger = logging.getLogger(__name__)


class PaymentWriteThrottleMixin:
    def get_throttles(self):
        throttles = super().get_throttles()
        if self.request.method in ('POST', 'PATCH', 'PUT', 'DELETE'):
            self.throttle_scope = 'payment'
            throttles.append(ScopedRateThrottle())
        return throttles


class CustomerPaymentMethodListCreateView(PaymentWriteThrottleMixin, generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    usage_type = UserPaymentMethod.UsageType.PAYMENT

    def get_queryset(self):
        return UserPaymentMethod.objects.filter(
            user=self.request.user,
            usage_type=self.usage_type,
            is_active=True,
        ).order_by('-is_default', '-created_at')

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return BankPaymentMethodCreateSerializer
        return UserPaymentMethodSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['usage_type'] = self.usage_type
        return context

    def get(self, request, *args, **kwargs):
        bankhub.expire_stale_attempts(user=request.user, usage_type=self.usage_type)
        data = UserPaymentMethodSerializer(self.get_queryset(), many=True).data
        return Response({'message': 'Lấy danh sách phương thức thanh toán thành công.', 'data': data})

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        method = serializer.save()
        data = UserPaymentMethodSerializer(method).data
        return Response(
            {'message': 'Thêm tài khoản ngân hàng thành công.', 'data': data},
            status=status.HTTP_201_CREATED,
        )


class CustomerPaymentMethodDetailView(PaymentWriteThrottleMixin, generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    usage_type = UserPaymentMethod.UsageType.PAYMENT

    def get_object(self):
        return get_object_or_404(
            UserPaymentMethod.objects,
            pk=self.kwargs['pk'],
            user=self.request.user,
            usage_type=self.usage_type,
            is_active=True,
        )

    def get(self, request, *args, **kwargs):
        return Response({
            'message': 'Lấy phương thức thanh toán thành công.',
            'data': UserPaymentMethodSerializer(self.get_object()).data,
        })

    def patch(self, request, *args, **kwargs):
        method = self.get_object()
        serializer = UserPaymentMethodUpdateSerializer(method, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({
            'message': 'Cập nhật phương thức thanh toán thành công.',
            'data': UserPaymentMethodSerializer(method).data,
        })

    def delete(self, request, *args, **kwargs):
        soft_delete_payment_method(self.get_object())
        return Response({'message': 'Xóa phương thức thanh toán thành công.'})


class CustomerPaymentMethodSetDefaultView(PaymentWriteThrottleMixin, generics.GenericAPIView):
    permission_classes = [IsCustomerRole]
    usage_type = UserPaymentMethod.UsageType.PAYMENT

    def patch(self, request, *args, **kwargs):
        method = get_object_or_404(
            UserPaymentMethod.objects,
            pk=self.kwargs['pk'],
            user=request.user,
            usage_type=self.usage_type,
            is_active=True,
        )
        set_default_payment_method(method)
        return Response({
            'message': 'Đặt phương thức mặc định thành công.',
            'data': UserPaymentMethodSerializer(method).data,
        })


class CustomerPaymentMethodOptionsView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]

    def get(self, request, *args, **kwargs):
        options = [
            {'code': 'MOMO', 'name': 'Ví MoMo', 'status': 'COMING_SOON'},
            {'code': 'VNPAY', 'name': 'VNPAY', 'status': 'COMING_SOON'},
            {'code': 'BANK_ACCOUNT', 'name': 'Tài khoản ngân hàng', 'status': 'AVAILABLE'},
        ]
        return Response({'message': 'Lấy danh sách lựa chọn thành công.', 'data': options})


class CustomerBankCatalogView(generics.GenericAPIView):
    permission_classes = [IsCustomerRole]

    def get(self, request, *args, **kwargs):
        return Response({
            'message': 'Lấy danh sách ngân hàng thành công.',
            'data': get_bank_catalog(),
        })


class WorkerPaymentMethodListCreateView(CustomerPaymentMethodListCreateView):
    permission_classes = [IsWorkerRole]
    usage_type = UserPaymentMethod.UsageType.PAYOUT


class WorkerPaymentMethodDetailView(CustomerPaymentMethodDetailView):
    permission_classes = [IsWorkerRole]
    usage_type = UserPaymentMethod.UsageType.PAYOUT


class WorkerPaymentMethodSetDefaultView(CustomerPaymentMethodSetDefaultView):
    permission_classes = [IsWorkerRole]
    usage_type = UserPaymentMethod.UsageType.PAYOUT


class WorkerPaymentMethodOptionsView(CustomerPaymentMethodOptionsView):
    permission_classes = [IsWorkerRole]


class WorkerBankCatalogView(CustomerBankCatalogView):
    permission_classes = [IsWorkerRole]


class PaymentMethodVerificationView(PaymentWriteThrottleMixin, generics.GenericAPIView):
    """Start a TINGEE Sandbox bank-link request for a saved account."""
    throttle_scope = 'payment'

    def get_queryset(self):
        return UserPaymentMethod.objects.filter(
            user=self.request.user,
            usage_type=self.usage_type,
            method_type=UserPaymentMethod.MethodType.BANK_ACCOUNT,
            is_active=True,
        )

    def post(self, request, *args, **kwargs):
        method = get_object_or_404(self.get_queryset(), pk=self.kwargs['pk'])
        if method.verification_status == UserPaymentMethod.VerificationStatus.VERIFIED:
            return Response({'message': 'Tài khoản đã được xác minh.', 'data': {'status': 'VERIFIED'}})

        previous = method.verification_attempts.order_by('-created_at').first()
        identity = str(request.data.get('identity') or '').strip()
        mobile = str(request.data.get('mobile') or request.user.phone_number or '').strip()
        if not identity and previous and previous.identity_encrypted:
            identity = decrypt_value(previous.identity_encrypted)
        if not mobile and previous and previous.mobile_encrypted:
            mobile = decrypt_value(previous.mobile_encrypted)
        if not identity:
            try:
                identity = str(request.user.worker_profile.identity_number or '').strip()
            except ObjectDoesNotExist:
                identity = ''
        if not identity or not mobile:
            return Response({
                'message': 'Cần nhập số CCCD/CMND và số điện thoại nhận OTP để xác minh Sandbox.',
            }, status=status.HTTP_400_BAD_REQUEST)
        if not re.fullmatch(r'\d{9,14}', identity) or not re.fullmatch(r'0\d{9}', mobile):
            return Response({
                'message': 'Số CCCD/CMND hoặc số điện thoại không đúng định dạng.',
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = bankhub.create_verification(method=method, identity=identity, mobile=mobile)
        except bankhub.BankHubError as exc:
            return Response({'message': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        confirm_id = str(result.get('confirmId') or '')
        authorize_url = result.get('authorizeLink') or result.get('deepLink') or ''
        if authorize_url:
            return Response({
                'message': 'Ngân hàng yêu cầu xác thực qua ứng dụng hoặc webhook; luồng này chưa bật trong bản Sandbox CleanWise.',
            }, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        with transaction.atomic():
            locked_method = UserPaymentMethod.objects.select_for_update().get(pk=method.pk)
            for old_attempt in locked_method.verification_attempts.filter(status=BankAccountVerification.Status.PENDING):
                old_attempt.status = BankAccountVerification.Status.EXPIRED
                old_attempt.identity_encrypted = ''
                old_attempt.mobile_encrypted = ''
                old_attempt.save(update_fields=['status', 'identity_encrypted', 'mobile_encrypted', 'updated_at'])
            attempt = BankAccountVerification.objects.create(
                payment_method=locked_method,
                link_token_xid=confirm_id or str(uuid.uuid4()),
                expires_at=timezone.now() + timedelta(minutes=4) if confirm_id else None,
                identity_encrypted=encrypt_value(identity),
                mobile_encrypted=encrypt_value(mobile),
            )
            if confirm_id:
                locked_method.verification_status = UserPaymentMethod.VerificationStatus.PENDING
                locked_method.save(update_fields=['verification_status', 'updated_at'])
            else:
                bankhub._mark_sandbox_verified(attempt)
        return Response({
            'message': 'Đã bắt đầu xác minh tài khoản qua TINGEE Sandbox.',
            'data': {
                'status': 'PENDING' if confirm_id else 'SANDBOX_VERIFIED',
                'requires_otp': bool(confirm_id),
                'verification_id': attempt.pk,
                'expires_at': attempt.expires_at,
                'otp_method': result.get('otpMethod', ''),
            },
        }, status=status.HTTP_201_CREATED if confirm_id else status.HTTP_200_OK)


class PaymentMethodVerificationConfirmView(PaymentWriteThrottleMixin, generics.GenericAPIView):
    throttle_scope = 'payment'

    def get_queryset(self):
        return UserPaymentMethod.objects.filter(
            user=self.request.user,
            usage_type=self.usage_type,
            method_type=UserPaymentMethod.MethodType.BANK_ACCOUNT,
            is_active=True,
        )

    def post(self, request, *args, **kwargs):
        method = get_object_or_404(self.get_queryset(), pk=self.kwargs['pk'])
        otp = str(request.data.get('otp') or '').strip()
        if not re.fullmatch(r'\d{4,8}', otp):
            return Response({'message': 'Mã OTP phải gồm từ 4 đến 8 chữ số.'}, status=400)
        attempt = method.verification_attempts.filter(
            status=BankAccountVerification.Status.PENDING,
        ).order_by('-created_at').first()
        if not attempt:
            return Response({'message': 'Không tìm thấy phiên xác minh đang chờ.'}, status=404)
        if attempt.expires_at and attempt.expires_at <= timezone.now():
            attempt.status = BankAccountVerification.Status.EXPIRED
            attempt.identity_encrypted = ''
            attempt.mobile_encrypted = ''
            attempt.save(update_fields=['status', 'identity_encrypted', 'mobile_encrypted', 'updated_at'])
            method.verification_status = UserPaymentMethod.VerificationStatus.UNVERIFIED
            method.save(update_fields=['verification_status', 'updated_at'])
            return Response({'message': 'Mã OTP đã hết hạn. Hãy bắt đầu xác minh lại.'}, status=400)
        try:
            bankhub.confirm_verification(method=method, confirm_id=attempt.link_token_xid, otp=otp)
        except bankhub.BankHubError as exc:
            return Response({'message': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            locked_attempt = BankAccountVerification.objects.select_for_update().get(pk=attempt.pk)
            locked_method = UserPaymentMethod.objects.select_for_update().get(pk=method.pk)
            bankhub._mark_sandbox_verified(locked_attempt)
            locked_method.refresh_from_db(fields=['verification_status'])
        return Response({
            'message': 'Tài khoản đã được xác minh trong môi trường TINGEE Sandbox.',
            'data': {'status': 'SANDBOX_VERIFIED'},
        })


class CustomerPaymentMethodVerificationView(PaymentMethodVerificationView):
    permission_classes = [IsCustomerRole]
    usage_type = UserPaymentMethod.UsageType.PAYMENT


class WorkerPaymentMethodVerificationView(PaymentMethodVerificationView):
    permission_classes = [IsWorkerRole]
    usage_type = UserPaymentMethod.UsageType.PAYOUT


class CustomerPaymentMethodVerificationConfirmView(PaymentMethodVerificationConfirmView):
    permission_classes = [IsCustomerRole]
    usage_type = UserPaymentMethod.UsageType.PAYMENT


class WorkerPaymentMethodVerificationConfirmView(PaymentMethodVerificationConfirmView):
    permission_classes = [IsWorkerRole]
    usage_type = UserPaymentMethod.UsageType.PAYOUT


class PayOSWebhookView(APIView):
    authentication_classes = []  # header Authorization lạ không được làm webhook trả 401
    permission_classes = [AllowAny]
    throttle_classes = []

    def post(self, request, *args, **kwargs):
        try:
            webhook_data = webhook_service.verify_and_parse_payos_webhook(request.body)
        except WebhookError:
            return Response(
                {'success': False, 'message': 'Chữ ký không hợp lệ.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception:
            logger.exception('Webhook payOS parse lỗi')
            return Response({'success': False}, status=status.HTTP_400_BAD_REQUEST)

        # Lỗi xử lý nghiệp vụ -> để nổ 500 để payOS tự gửi lại webhook (các hàm xử lý đều idempotent).
        payment = webhook_service.handle_payos_webhook(webhook_data)
        return Response({'success': True, 'payment_status': payment.status if payment else None})
    
    
def _positive_int(raw, default, maximum):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, 1), maximum)


class CustomerPaymentHistoryView(APIView):
    """GET lịch sử thanh toán đơn (online / ví) và hoàn tiền. ?source=all|wallet|online&page=&page_size="""
    permission_classes = [IsCustomerRole]

    def get(self, request, *args, **kwargs):
        source = request.query_params.get('source', 'all')
        if source not in history_service.SOURCES:
            raise ValidationError({'source': 'Giá trị không hợp lệ.'})
        status_filter = request.query_params.get('status', 'all')
        if status_filter not in history_service.STATUSES:
            raise ValidationError({'status': 'Giá trị không hợp lệ.'})
        data = history_service.get_payment_history(
            user=request.user,
            source=source,
            status=status_filter,
            page=_positive_int(request.query_params.get('page'), 1, 1000),
            page_size=_positive_int(request.query_params.get('page_size'), 20, 50),
        )
        return Response({'message': 'Lấy lịch sử thanh toán thành công.', 'data': data})
