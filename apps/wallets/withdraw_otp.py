import hashlib
import hmac
import secrets

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from rest_framework import serializers

OTP_TTL = 300
MAX_TRIES = 5
RESEND_COOLDOWN = 60


def _key(user, name):
    return f'wd_otp:{user.pk}:{name}'


def _hash(user, otp):
    return hmac.new(settings.SECRET_KEY.encode(), f'{user.pk}:{otp}'.encode(), hashlib.sha256).hexdigest()


def send_withdraw_otp(user):
    if not user.email:
        raise serializers.ValidationError({'detail': 'Tài khoản chưa có email để nhận mã OTP.'})
    if not cache.add(_key(user, 'cool'), 1, RESEND_COOLDOWN):
        raise serializers.ValidationError({'detail': f'Vui lòng đợi {RESEND_COOLDOWN} giây rồi gửi lại mã.'})

    otp = f'{secrets.randbelow(10**6):06d}'
    cache.set(_key(user, 'hash'), _hash(user, otp), OTP_TTL)
    cache.set(_key(user, 'tries'), 0, OTP_TTL)
    send_mail(
        subject='CleanWise - Mã xác nhận rút tiền',
        message=f'Mã OTP rút tiền của bạn là {otp}. Mã có hiệu lực {OTP_TTL // 60} phút. '
                f'Không chia sẻ mã này cho bất kỳ ai.',
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


def verify_withdraw_otp(user, otp):
    """Đúng thì tiêu hủy mã (dùng 1 lần). Sai/hết hạn/quá số lần -> ValidationError."""
    otp = str(otp or '').strip()
    if not otp.isdigit() or len(otp) != 6:
        raise serializers.ValidationError({'otp': 'Vui lòng nhập mã OTP gồm 6 số.'})

    expected = cache.get(_key(user, 'hash'))
    if not expected:
        raise serializers.ValidationError({'otp': 'Mã OTP đã hết hạn hoặc chưa được gửi.'})
    if int(cache.get(_key(user, 'tries'), 0)) >= MAX_TRIES:
        cache.delete(_key(user, 'hash'))
        raise serializers.ValidationError({'otp': 'Nhập sai quá nhiều lần, vui lòng gửi lại mã mới.'})

    if not hmac.compare_digest(expected, _hash(user, otp)):
        try:
            cache.incr(_key(user, 'tries'))
        except ValueError:
            cache.set(_key(user, 'tries'), 1, OTP_TTL)
        raise serializers.ValidationError({'otp': 'Mã OTP không đúng.'})

    # delete trả True chỉ với 1 request: 2 request song song cùng OTP đúng thì chỉ 1 cái qua
    if not cache.delete(_key(user, 'hash')):
        raise serializers.ValidationError({'otp': 'Mã OTP đã được sử dụng.'})
    cache.delete(_key(user, 'tries'))