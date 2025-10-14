"""
Django settings for ecom
"""
from pathlib import Path
from decouple import config, Csv
import os

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("DJANGO_SECRET_KEY")
DEBUG      = config("DJANGO_DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", cast=Csv())

INSTALLED_APPS = [
    # Django
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 3rd-party
    "rest_framework",
    "django_filters",
    "corsheaders",
    "rest_framework.authtoken",
    # Local
    "ecommerceapp",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",  # must be first
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "ecommerce.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "ecommerce.wsgi.application"

# --- Database ---
DB_ENGINE = config("DB_ENGINE", default="django.db.backends.sqlite3")
DB_NAME   = config("DB_NAME",   default="db.sqlite3")
DB_USER   = config("DB_USER",   default="")
DB_PASS   = config("DB_PASS",   default="")
DB_HOST   = config("DB_HOST",   default="")
DB_PORT   = config("DB_PORT",   default="")

if "sqlite" in DB_ENGINE:
    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINE,
            "NAME": str(BASE_DIR / DB_NAME),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINE,
            "NAME": DB_NAME,
            "USER": DB_USER,
            "PASSWORD": DB_PASS,
            "HOST": DB_HOST,
            "PORT": DB_PORT,
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE     = "UTC"
USE_I18N      = True
USE_TZ        = True

STATIC_URL  = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL   = "/media/"
MEDIA_ROOT  = BASE_DIR / "media"

# Optional S3 storage (leave blank for local)
if config("AWS_ACCESS_KEY_ID", default=""):
    INSTALLED_APPS += ["storages"]
    DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
    AWS_ACCESS_KEY_ID       = config("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY   = config("AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = config("AWS_STORAGE_BUCKET_NAME")

# Email (example: Gmail SMTP)
EMAIL_BACKEND       = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST          = config("EMAIL_HOST", default="")
EMAIL_HOST_USER     = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
EMAIL_PORT          = config("EMAIL_PORT", default=587, cast=int)
EMAIL_USE_TLS       = config("EMAIL_USE_TLS", default=True, cast=bool)
DEFAULT_FROM_EMAIL  = config("DEFAULT_FROM_EMAIL", default=EMAIL_HOST_USER)
LEAD_NOTIFY_EMAILS  = config("LEAD_NOTIFY_EMAILS", default="")
BACKEND_NOTIFY_EMAILS = config("BACKEND_NOTIFY_EMAILS", default="")
CONTACT_NOTIFY_EMAILS = config("CONTACT_NOTIFY_EMAILS", default="")
# CORS / CSRF — allow your React origin(s)
CORS_ALLOWED_ORIGINS = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]
CORS_ALLOW_CREDENTIALS = True  # safe to enable, even if not used

# If you’re posting JSON or Authorization:
CORS_ALLOW_HEADERS = [
    "authorization",
    "content-type",
    "accept",
    "origin",
    "user-agent",
    "dnt",
    "cache-control",
    "x-requested-with",
]

CSRF_TRUSTED_ORIGINS = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]

RAZORPAY_KEY_ID        = config("RAZORPAY_KEY_ID", default="")
RAZORPAY_KEY_SECRET    = config("RAZORPAY_KEY_SECRET", default="")
RAZORPAY_WEBHOOK_SECRET= config("RAZORPAY_WEBHOOK_SECRET", default="")  # optional but recommended


AUTH_USER_MODEL = "ecommerceapp.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ],
    "DEFAULT_PAGINATION_CLASS": "ecommerceapp.pagination.StandardResultsSetPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ],
}

# Security hardening in prod
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# Logging (optional)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler", "level": "DEBUG"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": True},
        __name__: {"handlers": ["console"], "level": "DEBUG", "propagate": False},
    },
}

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
CURRENCY_SYMBOL  = os.getenv("CURRENCY_SYMBOL", "$")
