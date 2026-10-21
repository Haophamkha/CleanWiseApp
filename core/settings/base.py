import os
from pathlib import Path
from datetime import time, timedelta
from corsheaders.defaults import default_headers
from celery.schedules import crontab

# Đường dẫn gốc tới thư mục project (nhảy lên 3 cấp từ core/settings/base.py)
BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-change-this-in-production')
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
# Local chat moderation; cosine scores are similarities, not probabilities.
CHAT_MODERATION_COSINE_THRESHOLD = float(os.environ.get('CHAT_MODERATION_COSINE_THRESHOLD', '0.4'))
CHAT_MODERATION_COSINE_MARGIN = float(os.environ.get('CHAT_MODERATION_COSINE_MARGIN', '0.12'))
PASSWORD_RESET_OTP_TTL_MINUTES = int(os.environ.get("PASSWORD_RESET_OTP_TTL_MINUTES", 10))
PASSWORD_RESET_OTP_MAX_ATTEMPTS = int(os.environ.get("PASSWORD_RESET_OTP_MAX_ATTEMPTS", 5))
FIELD_ENCRYPTION_KEY = os.environ.get("FIELD_ENCRYPTION_KEY", "")

# Các mutation quan trọng từ dashboard gửi Idempotency-Key để tránh thao tác
# trùng. Header tùy chỉnh phải được khai báo rõ cho CORS preflight.
CORS_ALLOW_HEADERS = (*default_headers, 'idempotency-key', 'ngrok-skip-browser-warning')

PAYOS_CLIENT_ID = os.environ.get("PAYOS_CLIENT_ID", "")
PAYOS_API_KEY = os.environ.get("PAYOS_API_KEY", "")
PAYOS_CHECKSUM_KEY = os.environ.get("PAYOS_CHECKSUM_KEY", "")
PAYOS_RETURN_URL = os.environ.get("PAYOS_RETURN_URL", "https://cleanwise.vn/payment/return")
PAYOS_CANCEL_URL = os.environ.get("PAYOS_CANCEL_URL", "https://cleanwise.vn/payment/cancel")  

PAYOUT_MODE = os.environ.get("PAYOUT_MODE", "mock")
TOPUP_MODE = os.environ.get("TOPUP_MODE", "mock")
PAYOUT_ALLOW_MOCK = os.environ.get("PAYOUT_ALLOW_MOCK") == "1"

# TINGEE Bank Link UAT; production credentials are intentionally not configured here.
TINGEE_BASE_URL = os.environ.get('TINGEE_BASE_URL', 'https://uat-open-api.tingee.vn')
TINGEE_CLIENT_ID = os.environ.get('TINGEE_CLIENT_ID', '')
TINGEE_SECRET_TOKEN = os.environ.get('TINGEE_SECRET_TOKEN', '')

PAYOS_PAYOUT_CLIENT_ID = os.environ.get("PAYOS_PAYOUT_CLIENT_ID", "")
PAYOS_PAYOUT_API_KEY = os.environ.get("PAYOS_PAYOUT_API_KEY", "")
PAYOS_PAYOUT_CHECKSUM_KEY = os.environ.get("PAYOS_PAYOUT_CHECKSUM_KEY", "")

WALLET_TOPUP_MIN = int(os.environ.get("WALLET_TOPUP_MIN", 50000))
WALLET_TOPUP_MAX = int(os.environ.get("WALLET_TOPUP_MAX", 20000000))
WALLET_WITHDRAW_MIN = int(os.environ.get("WALLET_WITHDRAW_MIN", 50000))
WALLET_WITHDRAW_MAX = int(os.environ.get("WALLET_WITHDRAW_MAX", 20000000))
WALLET_WITHDRAW_DAILY_MAX = int(os.environ.get("WALLET_WITHDRAW_DAILY_MAX", 50000000))


# ====================================


REDIS_URL = os.environ.get('REDIS_URL')

if REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
            'KEY_PREFIX': 'cw',
            'TIMEOUT': 300,
        }
    }
else:
    CACHES = {
        'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}
    }
    
# # ============================================= Danh sách Django Apps & Third-party Packages # ============================================= 
INSTALLED_APPS = [
    'daphne',
    'channels',
    # Django core apps
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party packages
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'drf_spectacular',

    # Local apps
    'apps.authentication',
    'apps.addresses',
    'apps.worker',
    'apps.payments',
    'apps.reviews',
    'apps.complaints',
    'apps.notifications',
    'apps.chat',
    'apps.services',
    'apps.bookings',
    'apps.vouchers',
    'apps.ai_engine',
    'apps.chatbot',
    'apps.analytics',
    'apps.common',
    'apps.wallets',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'
ASGI_APPLICATION = 'core.asgi.application'
AUTH_USER_MODEL = 'authentication.User'

# Redis is required when multiple ASGI processes serve WebSocket clients.
# The in-memory option is only for local development and tests.
if os.environ.get('CHAT_USE_IN_MEMORY_LAYER') == '1':
    CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}
else:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': [os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0')]},
        },
    }

# ============================================= Password validation=============================================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

#============================================= Internationalization =============================================
LANGUAGE_CODE = 'vi-vn'
TIME_ZONE = 'Asia/Ho_Chi_Minh'
USE_I18N = True
USE_TZ = True

# ============================================= Static & Media files =============================================
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
CUSTOMER_AVATAR_UPLOAD_DIR = os.environ.get(
    "CUSTOMER_AVATAR_UPLOAD_DIR",
    "customer_avatars"
)
CUSTOMER_AVATAR_MAX_SIZE = int(os.environ.get("CUSTOMER_AVATAR_MAX_SIZE", 5 * 1024 * 1024))
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")
CLOUDINARY_ROOT_FOLDER = "cleanwise"
CLOUDINARY_CUSTOMER_AVATAR_FOLDER = f"{CLOUDINARY_ROOT_FOLDER}/customer_avatars"
CLOUDINARY_SERVICE_IMAGE_FOLDER = f"{CLOUDINARY_ROOT_FOLDER}/service_images"
CLOUDINARY_REVIEW_IMAGE_FOLDER = f"{CLOUDINARY_ROOT_FOLDER}/review_images"
CLOUDINARY_WORKER_PROFILE_FOLDER = f"{CLOUDINARY_ROOT_FOLDER}/worker_profiles"
CLOUDINARY_SERVICE_ICON_FOLDER = f"{CLOUDINARY_ROOT_FOLDER}/service_icons"
WORKER_DOCUMENT_MAX_SIZE = int(os.environ.get("WORKER_DOCUMENT_MAX_SIZE", 10 * 1024 * 1024))
REVIEW_IMAGE_MAX_SIZE = int(os.environ.get("REVIEW_IMAGE_MAX_SIZE", 5 * 1024 * 1024))
REVIEW_MAX_IMAGES = int(os.environ.get("REVIEW_MAX_IMAGES", 5))

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


CHECKIN_MAX_DISTANCE_METERS = 300
CHECKIN_MAX_ACCURACY_METERS = 100
CHECKIN_ALLOW_MISSING_ADDRESS_COORDS = True

# ============================================= Email =============================================
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "True") == "True"
DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL",
    EMAIL_HOST_USER or "no-reply@cleanwise.local"
)

# =============================================Cấu hình Django REST Framework (DRF)============================================= 
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    
    # !! Bọc khi API gặp LỖI
    'EXCEPTION_HANDLER': 'apps.common.exceptions.custom_exception_handler',
    
    # !! Bọc khi API THÀNH CÔNG (Trả data hợp lệ)
    'DEFAULT_RENDERER_CLASSES': (
        'apps.common.renderers.CustomJSONRenderer',
        'rest_framework.renderers.BrowsableAPIRenderer', # Giữ lại giao diện DRF Web
    ),
    'DEFAULT_THROTTLE_CLASSES': (
    'rest_framework.throttling.AnonRateThrottle',
    'rest_framework.throttling.UserRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'anon': '60/min',
        'user': '120/min',
        'auth': '15/min',
        'otp': '10/min',
        'payment': '10/min',
        'booking': '10/min',
        'voucher': '20/min',
        'worker_action': '60/min',
        'upload': '10/min',
        'content': '10/min',
        'chat': '60/min',
        'chatbot': os.environ.get('CHATBOT_RATE_LIMIT', '10/min'),
        'device': '10/min',
    },
    'NUM_PROXIES': int(os.environ.get('NUM_PROXIES', 0)),
}

# M1: read-only customer assistant. Set a Gemini key before enabling real replies.
CHATBOT_ENABLED = os.environ.get('CHATBOT_ENABLED', '1') == '1'
CHATBOT_MODEL = os.environ.get('CHATBOT_MODEL', 'gemini-3.5-flash-lite')
CHATBOT_CHECKPOINT_DB_HOST = os.environ.get('CHATBOT_CHECKPOINT_DB_HOST', '')
CHATBOT_GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY') or os.environ.get('GEMINI_API_KEY', '')
CHATBOT_MODEL_TIMEOUT = int(os.environ.get('CHATBOT_MODEL_TIMEOUT', '20'))
CHATBOT_RUN_TIMEOUT = int(os.environ.get('CHATBOT_RUN_TIMEOUT', '90'))
CHATBOT_MAX_MODEL_CALLS = int(os.environ.get('CHATBOT_MAX_MODEL_CALLS', '10'))
CHATBOT_MAX_TOOL_CALLS = int(os.environ.get('CHATBOT_MAX_TOOL_CALLS', '12'))
CHATBOT_MAX_OUTPUT_TOKENS = int(os.environ.get('CHATBOT_MAX_OUTPUT_TOKENS', '4096'))
CHATBOT_HISTORY_TURNS = int(os.environ.get('CHATBOT_HISTORY_TURNS', '12'))
CHATBOT_HISTORY_CHARACTERS = int(os.environ.get('CHATBOT_HISTORY_CHARACTERS', '24000'))
CHATBOT_EMBED_MODEL = os.environ.get('CHATBOT_EMBED_MODEL', 'gemini-embedding-001')
CHATBOT_EMBED_DIM = int(os.environ.get('CHATBOT_EMBED_DIM', '768'))
CHATBOT_RAG_MAX_DISTANCE = float(os.environ.get('CHATBOT_RAG_MAX_DISTANCE', '0.40'))

# ============================================= Cấu hình Simple JWT=============================================
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}





# ============================================= Cấu hình Swagger API Documentation=============================================
SPECTACULAR_SETTINGS = {
    'TITLE': 'CleanWiseApp Backend API',
    'VERSION': '1.0.0',
    'TAGS': [
        # --- NHÓM 1: Xác thực & Dùng chung ---
        {'name': 'Auth', 'description': 'Đăng nhập, đăng ký chung (Khách hàng & Nhân viên)'},
        {'name': 'Auth - Password Reset', 'description': 'Quên và đặt lại mật khẩu'},

        # --- NHÓM 2: Dành cho Khách hàng (Customer) ---
        {'name': 'Customer - Profile', 'description': 'Thông tin cá nhân khách hàng'},
        {'name': 'Customer - Addresses', 'description': 'Quản lý địa chỉ khách hàng'},
        {'name': 'Services', 'description': 'Xem danh sách dịch vụ'},
        {'name': 'Voucher - Customer', 'description': 'Ví voucher và ưu đãi của khách hàng'},
        {'name': 'Booking - Customer', 'description': 'Đặt lịch dịch vụ của khách hàng'},
        {'name': 'Review - Customer', 'description': 'Đánh giá nhân viên theo từng buổi đã hoàn thành'},

        # --- NHÓM 3: Dành cho Nhân viên (Worker) ---
        {'name': 'Worker - Auth', 'description': 'Đăng ký tài khoản nhân viên'},
        {'name': 'Worker - Profile', 'description': 'Hồ sơ cá nhân nhân viên'},
        {'name': 'Worker - Areas', 'description': 'Khu vực hoạt động khả dụng'},
        {'name': 'Worker - Working Areas', 'description': 'Đăng ký khu vực làm việc của nhân viên'},
        {'name': 'Worker - Schedules', 'description': 'Lịch làm việc của nhân viên'},
        {'name': 'Worker - Assignments', 'description': 'Nhận và hủy việc của nhân viên'},
        {'name': 'Review - Worker', 'description': 'Xem đánh giá và thống kê điểm của nhân viên'},

        # --- NHÓM 4: Dành cho Quản trị viên (Admin) ---
        {'name': 'Users - Admin', 'description': 'Quản lý người dùng hệ thống'},
        {'name': 'Worker - Admin', 'description': 'Phê duyệt hồ sơ nhân viên'},
        {'name': 'Services - Admin', 'description': 'Quản lý dịch vụ'},
        {'name': 'Booking - Admin', 'description': 'Quản lý lịch làm việc & phân công'},
        {'name': 'Voucher - Admin', 'description': 'Quản lý, tạo và tra cứu voucher'},
        {'name': 'Review - Admin', 'description': 'Kiểm duyệt và phản hồi đánh giá'},
    ],
    'EXTENSIONS_ROOT': {
        'x-tagGroups': [
            {
                'name': '1. Xác thực & Dùng chung',
                'tags': ['Auth', 'Auth - Password Reset'],
            },
            {
                'name': '2. Khách hàng (Customer)',
                'tags': ['Customer - Profile', 'Customer - Addresses', 'Services', 'Voucher - Customer', 'Booking - Customer', 'Review - Customer'],
            },
            {
                'name': '3. Nhân viên (Worker)',
                'tags': ['Worker - Auth', 'Worker - Profile', 'Worker - Areas', 'Worker - Working Areas', 'Worker - Schedules', 'Worker - Assignments', 'Review - Worker'],
            },
            {
                'name': '4. Quản trị viên (Admin)',
                'tags': ['Users - Admin', 'Worker - Admin', 'Services - Admin', 'Booking - Admin', 'Voucher - Admin', 'Review - Admin'],
            },
        ],
    },
    'ENUM_NAME_OVERRIDES': {
        'WorkerProfileStatusEnum': 'apps.authentication.models.WorkerProfile.Status',
        'UserVoucherStatusEnum': 'apps.vouchers.models.UserVoucher.Status',
    },
}

CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'amqp://cleanwise:cleanwise@127.0.0.1:5672//')
CELERY_TASK_ALWAYS_EAGER = os.environ.get('CELERY_EAGER') == '1'
CELERY_TASK_SERIALIZER = 'json'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TIMEZONE = 'Asia/Ho_Chi_Minh'
CELERY_TASK_PUBLISH_RETRY = False
CELERY_BROKER_CONNECTION_TIMEOUT = 1
CELERY_BROKER_TRANSPORT_OPTIONS = {
    'max_retries': 0,
    'interval_start': 0,
    'interval_step': 0,
    'interval_max': 0,
}
PUSH_NOTIFICATIONS_ENABLED = os.environ.get('PUSH_NOTIFICATIONS_ENABLED', '1') == '1'

CELERY_BEAT_SCHEDULE = {
    'expire-unclaimed-schedules': {
        'task': 'apps.worker.tasks.expire_unclaimed_schedules_task',
        'schedule': 60.0,
        'options': {'expires': 55},
    },

    'handle-missed-checkouts': {
        'task': 'apps.worker.tasks.handle_missed_checkouts_task',
        'schedule': 60.0,
        'options': {'expires': 55},
    },

    'send-schedule-reminders': {
        'task': 'apps.worker.tasks.send_schedule_reminders_task',
        'schedule': 300.0,
        'options': {'expires': 290},
    },

    'notify-preferred-expired': {
        'task': 'apps.worker.tasks.notify_preferred_expired_task',
        'schedule': 60.0,
        'options': {'expires': 55},
    },

    'expire-unpaid-bookings': {
        'task': 'apps.worker.tasks.expire_unpaid_bookings_task',
        'schedule': 60.0,
        'options': {'expires': 55},
    },

    'release-held-earnings': {
        'task': 'apps.worker.tasks.release_held_earnings_task',
        'schedule': 600.0,
        'options': {'expires': 590},
    },

    'sync-withdraw-payouts': {
        'task': 'apps.wallets.tasks.sync_withdraw_payouts_task',
        'schedule': 60.0,
        'options': {'expires': 55},
    },

    'expire-wallet-topups': {
        'task': 'apps.wallets.tasks.expire_topups_task',
        'schedule': 300.0,
        'options': {'expires': 290},
    },

    'alert-stuck-withdraws': {
        'task': 'apps.wallets.tasks.alert_stuck_withdraws_task',
        'schedule': 300.0,
        'options': {'expires': 290},
    },
    'reconcile-wallets': {
        'task': 'apps.wallets.tasks.reconcile_wallets_task',
        'schedule': crontab(hour=2, minute=0),  # 2h sáng mỗi đêm (CELERY_TIMEZONE = Asia/Ho_Chi_Minh)
        'options': {'expires': 3600},
    },
    'process-pending-cash-payments': {
        'task': 'apps.worker.tasks.process_pending_cash_payments_task',
        'schedule': 600.0,
        'options': {'expires': 590},
    },
}


SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# =====================================================================================
# DÁN KHỐI NÀY VÀO CUỐI FILE core/settings/base.py (sau dòng SECURE_PROXY_SSL_HEADER).
# Các dòng WALLET_WITHDRAW_* ở đây ghi đè lại giá trị cũ ở phía trên (hạn mức giai đoạn đầu thấp hơn).
# LƯU Ý: nếu .env đang set WALLET_WITHDRAW_MAX / WALLET_WITHDRAW_DAILY_MAX thì giá trị trong .env vẫn thắng.
# =====================================================================================

# ----- Ví / rút tiền: hạn mức giai đoạn đầu, mở dần khi đối soát ổn -----
WALLET_WITHDRAW_MAX = int(os.environ.get("WALLET_WITHDRAW_MAX", 2000000))
WALLET_WITHDRAW_DAILY_MAX = int(os.environ.get("WALLET_WITHDRAW_DAILY_MAX", 5000000))

# Tài khoản ngân hàng mới thêm phải chờ bao lâu mới được rút về (test: đặt 0)
PAYOUT_METHOD_COOLDOWN_HOURS = int(os.environ.get("PAYOUT_METHOD_COOLDOWN_HOURS", 24))

# Khung giờ nhận lịch: giờ BẮT ĐẦU phải nằm trong khoảng này
BOOKING_OPEN_TIME = time(8, 0)
BOOKING_LAST_START_TIME = time(18, 0)

WITHDRAW_OTP_REQUIRED = os.environ.get('WITHDRAW_OTP_REQUIRED', '1') == '1'
