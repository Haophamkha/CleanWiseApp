"""Isolated local demo. Never use these settings for a deployed environment."""
from .test import *

DEBUG = True
SECRET_KEY = 'cleanwise-local-invitation-preview-only'
ALLOWED_HOSTS = ['localhost', '127.0.0.1', '10.0.2.2']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / '.local' / 'invitation-preview.sqlite3'}}
CORS_ALLOWED_ORIGINS = ['http://localhost:3010', 'http://127.0.0.1:3010', 'http://localhost:8082', 'http://127.0.0.1:8082']
CORS_ALLOW_CREDENTIALS = True
REST_FRAMEWORK = {**REST_FRAMEWORK, 'DEFAULT_THROTTLE_CLASSES': []}

# SQLite preview creates tables from current models. Production migration history
# contains PostgreSQL-specific SQL; production must use its normal migrations.
