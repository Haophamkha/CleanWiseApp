"""TINGEE Bank Link client for the UAT/sandbox bank-account flow."""
import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings
from django.utils import timezone

from apps.common.encryption import decrypt_value
from .models import BankAccountVerification, UserPaymentMethod

logger = logging.getLogger(__name__)


class BankHubError(Exception):
    pass


def _configured():
    return bool(
        settings.TINGEE_CLIENT_ID
        and settings.TINGEE_SECRET_TOKEN
        and settings.TINGEE_BASE_URL.rstrip('/') == 'https://uat-open-api.tingee.vn'
    )


def _signed_headers(body):
    timestamp = timezone.localtime().strftime('%Y%m%d%H%M%S%f')[:-3]
    body_json = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
    message = f'{timestamp}:{body_json}'.encode('utf-8')
    signature = hmac.new(
        settings.TINGEE_SECRET_TOKEN.encode('utf-8'), message, hashlib.sha512,
    ).hexdigest()
    return timestamp, body_json, {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'x-client-id': settings.TINGEE_CLIENT_ID,
        'x-request-timestamp': timestamp,
        'x-signature': signature,
    }


def _post(path, body):
    if not _configured():
        raise BankHubError('Chế độ xác minh Sandbox chỉ chấp nhận TINGEE UAT và credentials UAT.')
    _, body_json, headers = _signed_headers(body)
    try:
        response = requests.post(
            f'{settings.TINGEE_BASE_URL.rstrip("/")}{path}',
            data=body_json.encode('utf-8'), headers=headers, timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise BankHubError('TINGEE trả về dữ liệu không hợp lệ.')
    except BankHubError:
        raise
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.warning('TINGEE bank-link request %s failed (%s)', path, type(exc).__name__)
        raise BankHubError('Không kết nối được TINGEE Sandbox. Vui lòng thử lại.') from exc
    if str(payload.get('code')) != '00':
        logger.info('TINGEE rejected bank-link request: code=%s', payload.get('code'))
        raise BankHubError(payload.get('message') or 'TINGEE không thể xác minh tài khoản này.')
    data = payload.get('data')
    if not isinstance(data, dict):
        raise BankHubError('TINGEE trả về phiên xác minh không hợp lệ.')
    return data


def create_verification(*, method, identity, mobile):
    """Start a TINGEE bank-link request; OTP is confirmed in a second call."""
    try:
        account_number = decrypt_value(method.account_number_encrypted)
    except Exception as exc:
        logger.error('Cannot decrypt bank account for TINGEE verification (method=%s)', method.pk)
        raise BankHubError('Không đọc được số tài khoản đã lưu.') from exc

    body = {
        'bankBin': method.bank_bin,
        'accountType': 'personal-account',
        'accountNumber': account_number,
        'accountName': method.account_holder_name,
        'identity': identity,
        'mobile': mobile,
        # Request the original bank account where the selected bank supports it.
        'isNotifyAccountNumber': True,
    }
    return _post('/v1/create-va', body)


def confirm_verification(*, method, confirm_id, otp):
    return _post('/v1/confirm-va', {
        'bankBin': method.bank_bin,
        'confirmId': confirm_id,
        'otpNumber': otp,
    })


def _mark_sandbox_verified(attempt):
    method = attempt.payment_method
    method.verification_status = UserPaymentMethod.VerificationStatus.SANDBOX_VERIFIED
    method.save(update_fields=['verification_status', 'updated_at'])
    attempt.status = BankAccountVerification.Status.VERIFIED
    attempt.identity_encrypted = ''
    attempt.mobile_encrypted = ''
    attempt.save(update_fields=['status', 'identity_encrypted', 'mobile_encrypted', 'updated_at'])


def expire_stale_attempts(*, user, usage_type):
    now = timezone.now()
    stale = BankAccountVerification.objects.filter(
        payment_method__user=user,
        payment_method__usage_type=usage_type,
        payment_method__is_active=True,
        status=BankAccountVerification.Status.PENDING,
        expires_at__isnull=False,
        expires_at__lte=now,
    ).select_related('payment_method')
    for attempt in stale:
        attempt.status = BankAccountVerification.Status.EXPIRED
        attempt.identity_encrypted = ''
        attempt.mobile_encrypted = ''
        attempt.save(update_fields=['status', 'identity_encrypted', 'mobile_encrypted', 'updated_at'])
        if attempt.payment_method.verification_status == UserPaymentMethod.VerificationStatus.PENDING:
            attempt.payment_method.verification_status = UserPaymentMethod.VerificationStatus.UNVERIFIED
            attempt.payment_method.save(update_fields=['verification_status', 'updated_at'])
