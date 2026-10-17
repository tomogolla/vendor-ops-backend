from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ['localhost', '127.0.0.1']),
    CORS_ALLOWED_ORIGINS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
    DJANGO_SECURE_COOKIES=(bool, False),
    DJANGO_SECURE_SSL_REDIRECT=(bool, False),
    DJANGO_HSTS_SECONDS=(int, 0),
    BUSINESS_TIMEZONE=(str, 'America/New_York'),
    BUSINESS_DAYS=(list, ['0', '1', '2', '3', '4']),
    BUSINESS_OPEN=(str, '09:00'),
    BUSINESS_CLOSE=(str, '17:00'),
    DISCOVERY_CALL_DUE_BUSINESS_HOURS=(float, 2.0),
    # Email (Slice 1 foundation). Safe defaults: the console provider and sending disabled.
    EMAIL_PROVIDER=(str, 'console'),
    EMAIL_GLOBAL_MODE=(str, 'disabled'),
    EMAIL_FROM_NAME=(str, 'The Good Flea'),
    EMAIL_FROM_ADDRESS=(str, ''),
    EMAIL_REPLY_TO=(str, ''),
    EMAIL_REDIRECT_TO=(str, ''),
    EMAIL_TIMEZONE=(str, 'America/New_York'),
    EMAIL_SEND_WINDOW_START=(str, '09:00'),
    EMAIL_SEND_WINDOW_END=(str, '18:00'),
    EMAIL_SMTP_HOST=(str, 'smtp-relay.gmail.com'),
    EMAIL_SMTP_PORT=(int, 587),
    EMAIL_SMTP_USE_TLS=(bool, True),
    EMAIL_SMTP_USERNAME=(str, ''),
    EMAIL_SMTP_PASSWORD=(str, ''),
    EMAIL_SMTP_TIMEOUT=(int, 20),
    EMAIL_RESEND_API_KEY=(str, ''),
    EMAIL_RESEND_WEBHOOK_SECRET=(str, ''),
    EMAIL_DAILY_LIMIT=(int, 90),
)
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('DJANGO_SECRET_KEY')
DEBUG = env('DJANGO_DEBUG')
ALLOWED_HOSTS = env('DJANGO_ALLOWED_HOSTS')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'django_filters',
    'corsheaders',
    'accounts',
    'catalog',
    'vendors',
    'tasks',
    'imports',
    'anymail',
    'communications.apps.CommunicationsConfig',
    'public_api.apps.PublicApiConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {'default': env.db('DATABASE_URL')}
DATABASES['default']['ATOMIC_REQUESTS'] = True

AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- API ---------------------------------------------------------------------
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': ['config.authentication.SessionAuthentication401'],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],
    'DEFAULT_PAGINATION_CLASS': 'config.pagination.StandardPagination',
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.OrderingFilter',
        'rest_framework.filters.SearchFilter',
    ],
    'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
    'DEFAULT_THROTTLE_CLASSES': ['rest_framework.throttling.ScopedRateThrottle'],
    'DEFAULT_THROTTLE_RATES': {'login': '10/min'},
}

# --- Cookies, CORS and CSRF --------------------------------------------------
# In development the Vite dev server proxies /api to Django, so the browser sees a
# single origin. CORS is only needed if the frontend calls the API cross-origin.
CORS_ALLOWED_ORIGINS = env('CORS_ALLOWED_ORIGINS')
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = env('CSRF_TRUSTED_ORIGINS')
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = env('DJANGO_SECURE_COOKIES')
CSRF_COOKIE_SECURE = env('DJANGO_SECURE_COOKIES')
if env('DJANGO_SECURE_COOKIES'):
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = env('DJANGO_SECURE_SSL_REDIRECT')
SECURE_HSTS_SECONDS = env('DJANGO_HSTS_SECONDS')
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

# --- Business rules ----------------------------------------------------------
BUSINESS_HOURS = {
    'timezone': env('BUSINESS_TIMEZONE'),
    'days': [int(day) for day in env('BUSINESS_DAYS')],
    'open': env('BUSINESS_OPEN'),
    'close': env('BUSINESS_CLOSE'),
}
DISCOVERY_CALL_DUE_BUSINESS_HOURS = env('DISCOVERY_CALL_DUE_BUSINESS_HOURS')
MAX_CALL_ATTEMPTS = 5
# Money is stored as integer cents (`*_cents`); the currency code is for display only.
CURRENCY = 'USD'

# --- Email (communications app) ------------------------------------------------
# Django never sends from a web request: messages are queued in the database and the
# `send_due_emails` command (systemd timer) delivers them. See README "Email".
# Provider: `console` (Django's EMAIL_BACKEND; console locally, in-memory in tests),
# `smtp_relay` (Google Workspace SMTP relay) or `resend` (Resend API through django-anymail).
EMAIL_PROVIDER = env('EMAIL_PROVIDER')
# Ceiling for sending: disabled | redirect | enabled. The admin setting can only be stricter.
EMAIL_GLOBAL_MODE = env('EMAIL_GLOBAL_MODE')
EMAIL_FROM_NAME = env('EMAIL_FROM_NAME')
EMAIL_FROM_ADDRESS = env('EMAIL_FROM_ADDRESS')
EMAIL_REPLY_TO = env('EMAIL_REPLY_TO')
EMAIL_REDIRECT_TO = env('EMAIL_REDIRECT_TO')
EMAIL_TIMEZONE = env('EMAIL_TIMEZONE')
EMAIL_SEND_WINDOW_START = env('EMAIL_SEND_WINDOW_START')
EMAIL_SEND_WINDOW_END = env('EMAIL_SEND_WINDOW_END')
EMAIL_SMTP = {
    'host': env('EMAIL_SMTP_HOST'),
    'port': env('EMAIL_SMTP_PORT'),
    'use_tls': env('EMAIL_SMTP_USE_TLS'),
    'username': env('EMAIL_SMTP_USERNAME'),
    'password': env('EMAIL_SMTP_PASSWORD'),
    'timeout': env('EMAIL_SMTP_TIMEOUT'),
}
# Conservative delivery rules for the worker.
EMAIL_MAX_ATTEMPTS = 3
EMAIL_RETRY_DELAYS_MINUTES = [5, 30]
EMAIL_QUEUE_MAX_AGE_HOURS = 72
EMAIL_STUCK_SENDING_MINUTES = 15
# Messages sent per day (since local midnight in the business time zone); 0 = no limit.
EMAIL_DAILY_LIMIT = env('EMAIL_DAILY_LIMIT')
# After a provider rate-limit reply (HTTP 429), retry this much later without using an attempt.
EMAIL_RATE_LIMIT_DELAY_MINUTES = 1

# Resend through django-anymail. The API key and webhook signing secret come from the
# environment only. Anymail reads them from ANYMAIL; the webhook refuses every call while no
# signing secret is configured (communications/webhooks.py).
EMAIL_RESEND_BACKEND = 'anymail.backends.resend.EmailBackend'
ANYMAIL = {
    'RESEND_API_KEY': env('EMAIL_RESEND_API_KEY'),
    'RESEND_SIGNING_SECRET': env('EMAIL_RESEND_WEBHOOK_SECRET') or None,
}
# Used by the `console` provider. Never an SMTP backend: production delivery goes through the provider.
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
}
