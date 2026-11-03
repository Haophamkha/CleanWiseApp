from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import UserVoucher, Voucher


MONEY_QUANTIZER = Decimal('0.01')
User = get_user_model()


def _validate_claimable_voucher(voucher):
    now = timezone.now()
    if not voucher.is_active:
        raise serializers.ValidationError({'voucher': 'Voucher đã ngừng hoạt động.'})
    if now < voucher.start_at:
        raise serializers.ValidationError({'voucher': 'Voucher chưa đến thời gian nhận.'})
    if now > voucher.end_at:
        raise serializers.ValidationError({'voucher': 'Voucher đã hết hạn.'})
    if voucher.issuance_limit is not None and voucher.issued_count >= voucher.issuance_limit:
        raise serializers.ValidationError({'voucher': 'Voucher đã được nhận hết.'})


@transaction.atomic
def claim_voucher_by_code(*, code, customer):
    normalized_code = (code or '').strip().upper()
    if not normalized_code:
        raise serializers.ValidationError({'code': 'Vui lòng nhập mã voucher.'})
    try:
        voucher = Voucher.objects.select_for_update().get(code__iexact=normalized_code)
    except Voucher.DoesNotExist:
        raise serializers.ValidationError({'code': 'Mã voucher không tồn tại.'})

    source_by_distribution = {
        Voucher.DistributionType.PUBLIC: UserVoucher.Source.PUBLIC,
        Voucher.DistributionType.CODE_ONLY: UserVoucher.Source.CODE,
    }
    source = source_by_distribution.get(voucher.distribution_type)
    if source is None:
        raise serializers.ValidationError({'code': 'Voucher này chỉ được quản trị viên cấp riêng.'})
    return _claim_locked_voucher(voucher=voucher, customer=customer, source=source)


def _claim_locked_voucher(*, voucher, customer, source):
    _validate_claimable_voucher(voucher)
    if UserVoucher.objects.filter(user=customer, voucher=voucher).exists():
        raise serializers.ValidationError({'voucher': 'Bạn đã nhận voucher này rồi.'})
    user_voucher = UserVoucher.objects.create(user=customer, voucher=voucher, source=source)
    voucher.issued_count += 1
    voucher.save(update_fields=['issued_count', 'updated_at'])
    return user_voucher


@transaction.atomic
def assign_voucher_to_customer(*, voucher_id, customer_id, admin_user, note=None):
    try:
        voucher = Voucher.objects.select_for_update().get(pk=voucher_id)
    except Voucher.DoesNotExist:
        raise serializers.ValidationError({'voucher_id': 'Voucher không tồn tại.'})

    try:
        customer = User.objects.get(pk=customer_id, role=User.Role.CUSTOMER)
    except User.DoesNotExist:
        raise serializers.ValidationError({'customer_id': 'Khách hàng không tồn tại.'})

    if voucher.distribution_type != Voucher.DistributionType.ASSIGNED:
        raise serializers.ValidationError({
            'voucher_id': 'Chỉ voucher có hình thức phát hành ASSIGNED mới được cấp riêng.',
        })

    try:
        _validate_claimable_voucher(voucher)
    except serializers.ValidationError as exc:
        detail = exc.detail.get('voucher', exc.detail)
        raise serializers.ValidationError({'voucher_id': detail})

    if UserVoucher.objects.filter(user=customer, voucher=voucher).exists():
        raise serializers.ValidationError({'customer_id': 'Khách hàng đã được cấp voucher này.'})

    user_voucher = UserVoucher.objects.create(
        user=customer,
        voucher=voucher,
        source=UserVoucher.Source.ADMIN,
        assigned_by=admin_user,
        note=(note or '').strip() or None,
    )
    voucher.issued_count += 1
    voucher.save(update_fields=['issued_count', 'updated_at'])
    return user_voucher


def _to_money(value, field_name='subtotal_amount'):
    try:
        amount = Decimal(str(value)).quantize(MONEY_QUANTIZER, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise serializers.ValidationError({field_name: 'Số tiền không hợp lệ.'})
    if amount < 0:
        raise serializers.ValidationError({field_name: 'Số tiền không được âm.'})
    return amount


def calculate_voucher_discount(voucher, subtotal_amount):
    subtotal = _to_money(subtotal_amount)
    if voucher.discount_type == Voucher.DiscountType.PERCENT:
        discount = subtotal * voucher.discount_value / Decimal('100')
        if voucher.max_discount_amount is not None:
            discount = min(discount, voucher.max_discount_amount)
    else:
        discount = voucher.discount_value
    discount = min(discount, subtotal).quantize(MONEY_QUANTIZER, rounding=ROUND_HALF_UP)
    total = (subtotal - discount).quantize(MONEY_QUANTIZER, rounding=ROUND_HALF_UP)
    return {'subtotal_amount': subtotal, 'discount_amount': discount, 'total_amount': total}


def validate_and_calculate_voucher(
    *,
    code,
    customer,
    subtotal_amount,
    lock=False,
    error_field='code',
):
    normalized_code = (code or '').strip().upper()
    if not normalized_code:
        raise serializers.ValidationError({error_field: 'Vui lòng nhập mã voucher.'})
    if lock and not transaction.get_connection().in_atomic_block:
        raise RuntimeError('lock=True phải được gọi bên trong transaction.atomic().')

    queryset = UserVoucher.objects.select_related('voucher')
    if lock:
        queryset = queryset.select_for_update()
    user_voucher = queryset.filter(user=customer, voucher__code__iexact=normalized_code).first()
    if not user_voucher:
        raise serializers.ValidationError({error_field: 'Voucher chưa có trong ví của bạn.'})
    if user_voucher.status != UserVoucher.Status.AVAILABLE:
        status_messages = {
            UserVoucher.Status.RESERVED: 'Voucher đang được giữ cho một booking khác.',
            UserVoucher.Status.USED: 'Voucher đã được sử dụng.',
            UserVoucher.Status.REVOKED: 'Voucher đã bị thu hồi.',
        }
        raise serializers.ValidationError({
            error_field: status_messages.get(
                user_voucher.status,
                'Voucher không thể sử dụng.',
            ),
        })

    voucher = user_voucher.voucher
    now = timezone.now()
    if not voucher.is_active:
        raise serializers.ValidationError({error_field: 'Voucher đã ngừng hoạt động.'})
    if now < voucher.start_at:
        raise serializers.ValidationError({error_field: 'Voucher chưa đến thời gian sử dụng.'})
    if now > voucher.end_at:
        raise serializers.ValidationError({error_field: 'Voucher đã hết hạn.'})
    subtotal = _to_money(subtotal_amount)
    if subtotal < voucher.min_order_amount:
        raise serializers.ValidationError({
            error_field: f'Đơn dịch vụ tối thiểu phải đạt {voucher.min_order_amount}.',
        })
    return {'user_voucher': user_voucher, 'voucher': voucher, **calculate_voucher_discount(voucher, subtotal)}


def _locked_user_voucher(user_voucher_id):
    try:
        return UserVoucher.objects.select_for_update().get(pk=user_voucher_id)
    except UserVoucher.DoesNotExist:
        raise serializers.ValidationError({'voucher_code': 'Voucher của khách hàng không tồn tại.'})


@transaction.atomic
def reserve_user_voucher(*, user_voucher_id):
    """Giữ voucher cho booking vừa được tạo; gọi lặp khi đã RESERVED là an toàn."""
    user_voucher = _locked_user_voucher(user_voucher_id)
    if user_voucher.status == UserVoucher.Status.RESERVED:
        return user_voucher
    if user_voucher.status != UserVoucher.Status.AVAILABLE:
        raise serializers.ValidationError({'voucher_code': 'Voucher không còn khả dụng.'})

    user_voucher.status = UserVoucher.Status.RESERVED
    user_voucher.reserved_at = timezone.now()
    user_voucher.used_at = None
    user_voucher.save(update_fields=['status', 'reserved_at', 'used_at', 'updated_at'])
    return user_voucher


@transaction.atomic
def mark_user_voucher_used(*, user_voucher_id):
    """Chốt sử dụng voucher sau thanh toán hoặc khi booking được xác nhận."""
    user_voucher = _locked_user_voucher(user_voucher_id)
    if user_voucher.status == UserVoucher.Status.USED:
        return user_voucher
    if user_voucher.status != UserVoucher.Status.RESERVED:
        raise serializers.ValidationError({
            'voucher_code': 'Chỉ voucher đang được giữ mới có thể chuyển sang đã sử dụng.',
        })

    user_voucher.status = UserVoucher.Status.USED
    user_voucher.used_at = timezone.now()
    user_voucher.save(update_fields=['status', 'used_at', 'updated_at'])
    return user_voucher


@transaction.atomic
def release_user_voucher(*, user_voucher_id, allow_used=False):
    """Hoàn voucher cho ví; không thay đổi issued_count vì quyền sở hữu vẫn còn."""
    user_voucher = _locked_user_voucher(user_voucher_id)
    if user_voucher.status == UserVoucher.Status.AVAILABLE:
        return user_voucher

    releasable_statuses = {UserVoucher.Status.RESERVED}
    if allow_used:
        releasable_statuses.add(UserVoucher.Status.USED)
    if user_voucher.status not in releasable_statuses:
        raise serializers.ValidationError({
            'voucher_code': 'Voucher không thể được hoàn ở trạng thái hiện tại.',
        })

    user_voucher.status = UserVoucher.Status.AVAILABLE
    user_voucher.reserved_at = None
    user_voucher.used_at = None
    user_voucher.save(update_fields=[
        'status',
        'reserved_at',
        'used_at',
        'updated_at',
    ])
    return user_voucher
