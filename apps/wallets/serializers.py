from rest_framework import serializers

from .models import Wallet, WalletTopup, WalletTransaction, WithdrawRequest
from .payout_client import friendly_error

class WalletTransactionSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    booking_code = serializers.CharField(source='booking.booking_code', read_only=True, allow_null=True)

    class Meta:
        model = WalletTransaction
        fields = [
            'id', 'code', 'type', 'type_display', 'amount', 'balance_after',
            'status', 'status_display', 'booking_code', 'note', 'created_at', 'direction',
        ]
        
        
class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = ['id', 'balance', 'updated_at']


class WithdrawRequestSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=1000)
    # Bỏ trống -> dùng tài khoản ngân hàng mặc định
    payment_method_id = serializers.IntegerField(required=False, allow_null=True)


class TopupRequestSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=1000)


class WithdrawRecordSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    account_number_masked = serializers.CharField(read_only=True)
    failure_reason = serializers.SerializerMethodField()
    status_detail = serializers.SerializerMethodField()

    class Meta:
        model = WithdrawRequest
        fields = [
            'id', 'amount', 'status', 'status_display', 'status_detail', 'bank_name',
            'account_holder_name', 'account_number_masked', 'failure_reason',
            'needs_review', 'wallet_transaction_id', 'created_at', 'completed_at',
        ]

    def get_failure_reason(self, obj):
        if obj.status != WithdrawRequest.Status.FAILED:
            return ''
        return friendly_error(obj.failure_reason)

    def get_status_detail(self, obj):
        if obj.status != WithdrawRequest.Status.PROCESSING:
            return ''
        if obj.needs_review:
            return 'Giao dịch đang được đối soát. Tiền của bạn được giữ an toàn, liên hệ CSKH nếu quá 24 giờ.'
        if obj.had_unknown_result:
            return 'Chưa nhận được phản hồi từ ngân hàng, hệ thống đang kiểm tra lại.'
        return 'Đang gửi lệnh đến ngân hàng.'


class WalletTopupSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = WalletTopup
        fields = [
            'id', 'amount', 'status', 'status_display', 'checkout_url', 'qr_code',
            'payment_link_id', 'link_expires_at', 'paid_at', 'created_at',
        ]