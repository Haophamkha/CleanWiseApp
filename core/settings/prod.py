import os
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ImproperlyConfigured

from .base import *

DEBUG = False

SECRET_KEY = os.environ["SECRET_KEY"]

ALLOWED_HOSTS = [h for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h]
CORS_ALLOWED_ORIGINS = [o for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o]
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o]


def build_database_config():
    keepalive = {
        'keepalives': 1,
        'keepalives_idle': 30,
        'keepalives_interval': 10,
        'keepalives_count': 5,
    }
    database_url = os.environ.get('DATABASE_URL')
    if database_url:
        parsed_url = urlparse(database_url)
        query_params = parse_qs(parsed_url.query)
        return {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': parsed_url.path.lstrip('/'),
            'USER': parsed_url.username,
            'PASSWORD': parsed_url.password,
            'HOST': parsed_url.hostname,
            'PORT': parsed_url.port or '5432',
            'CONN_MAX_AGE': 60,
            'CONN_HEALTH_CHECKS': True,
            'OPTIONS': {'sslmode': query_params.get('sslmode', ['require'])[0], **keepalive},
        }
    return {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ['DB_NAME'],
        'USER': os.environ['DB_USER'],
        'PASSWORD': os.environ['DB_PASSWORD'],
        'HOST': os.environ['DB_HOST'],
        'PORT': os.environ.get('DB_PORT', '5432'),
        'CONN_MAX_AGE': 60,
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {'sslmode': os.environ.get('DB_SSLMODE', 'require'), **keepalive},
    }


DATABASES = {'default': build_database_config()}

# Sau reverse proxy (nginx) có HTTPS
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "0") == "1"
SESSION_COOKIE_SECURE = SECURE_SSL_REDIRECT
CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT


# ============================================= Chốt an toàn tiền: sai cấu hình thì KHÔNG khởi động =============================================
if DEBUG or PAYOUT_ALLOW_MOCK:
    raise ImproperlyConfigured('Production không được bật DEBUG / PAYOUT_ALLOW_MOCK.')

if PAYOUT_MODE != 'payos' or TOPUP_MODE != 'payos':
    raise ImproperlyConfigured('Production phải để PAYOUT_MODE=payos và TOPUP_MODE=payos.')

if SECRET_KEY.startswith('django-insecure'):
    raise ImproperlyConfigured('SECRET_KEY production đang là giá trị mặc định không an toàn.')

_required_settings = {
    'FIELD_ENCRYPTION_KEY': FIELD_ENCRYPTION_KEY,
    'PAYOS_CLIENT_ID': PAYOS_CLIENT_ID,
    'PAYOS_API_KEY': PAYOS_API_KEY,
    'PAYOS_CHECKSUM_KEY': PAYOS_CHECKSUM_KEY,
    'PAYOS_PAYOUT_CLIENT_ID': PAYOS_PAYOUT_CLIENT_ID,
    'PAYOS_PAYOUT_API_KEY': PAYOS_PAYOUT_API_KEY,
    'PAYOS_PAYOUT_CHECKSUM_KEY': PAYOS_PAYOUT_CHECKSUM_KEY,
    'REDIS_URL': REDIS_URL,  # idempotency + cache chống trùng cần Redis thật, không dùng locmem
}
_missing = [name for name, value in _required_settings.items() if not value]
if _missing:
    raise ImproperlyConfigured(f'Thiếu cấu hình bắt buộc cho production: {", ".join(_missing)}')


TOPUP_MODE = 'payos'
PAYOUT_MODE = 'payos'
PAYOUT_ALLOW_MOCK = False