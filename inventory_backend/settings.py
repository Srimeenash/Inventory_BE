"""
Django settings for inventory_backend project.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


# ---------------------------------------------------------------------
# Base Directory
# ---------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


# ---------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-p@%b#wzwaa^!uu_%*)&)1@+3-#zlzcbig^0)7ba4!v768ihs=a",
)

DEBUG = os.environ.get(
    "DJANGO_DEBUG",
    "True",
).lower() in (
    "true",
    "1",
    "yes",
)

ALLOWED_HOSTS = os.environ.get(
    "DJANGO_ALLOWED_HOSTS",
    "*",
).split(",")


# ---------------------------------------------------------------------
# Custom User Model
# ---------------------------------------------------------------------
AUTH_USER_MODEL = "users.User"


# ---------------------------------------------------------------------
# Installed Applications
# ---------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    "django_filters",
    "dashboard",
    "projects",
    "vendors",
    "components",
    "inventory",
    "procurement",
    "bom",
    "finance",
    "users",
    "roles",
    "reports",
    "notifications",
    "componentusage",
    "approvals",
    "materialrequest",
    "inward",
    "outward",
]


# ---------------------------------------------------------------------
# API PERFORMANCE AUDIT
# ---------------------------------------------------------------------
# Disabled by default.
#
# Enable temporarily with:
#     API_PERFORMANCE_LOGGING=True
#
# Optional:
#     API_PERFORMANCE_SLOW_QUERY_SECONDS=0.10
#     API_PERFORMANCE_TOP_DUPLICATES=5
#     API_PERFORMANCE_TOP_SLOW_QUERIES=5
#
# No models, serializers, views, workflow, status or pagination are changed.
API_PERFORMANCE_LOGGING = (
    os.environ.get(
        "API_PERFORMANCE_LOGGING",
        "False",
    ).lower()
    in (
        "true",
        "1",
        "yes",
        "on",
    )
)

try:
    API_PERFORMANCE_SLOW_QUERY_SECONDS = float(
        os.environ.get(
            "API_PERFORMANCE_SLOW_QUERY_SECONDS",
            "0.10",
        )
    )
except (TypeError, ValueError):
    API_PERFORMANCE_SLOW_QUERY_SECONDS = 0.10

try:
    API_PERFORMANCE_TOP_DUPLICATES = max(
        1,
        int(
            os.environ.get(
                "API_PERFORMANCE_TOP_DUPLICATES",
                "5",
            )
        ),
    )
except (TypeError, ValueError):
    API_PERFORMANCE_TOP_DUPLICATES = 5

try:
    API_PERFORMANCE_TOP_SLOW_QUERIES = max(
        1,
        int(
            os.environ.get(
                "API_PERFORMANCE_TOP_SLOW_QUERIES",
                "5",
            )
        ),
    )
except (TypeError, ValueError):
    API_PERFORMANCE_TOP_SLOW_QUERIES = 5


# ---------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if API_PERFORMANCE_LOGGING:
    MIDDLEWARE.append(
        "inventory_backend.performance_middleware."
        "ApiPerformanceLoggingMiddleware"
    )


# ---------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------
ROOT_URLCONF = "inventory_backend.urls"


# ---------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": (
            "django.template.backends.django.DjangoTemplates"
        ),
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                (
                    "django.template.context_processors.request"
                ),
                (
                    "django.contrib.auth.context_processors.auth"
                ),
                (
                    "django.contrib.messages.context_processors.messages"
                ),
            ],
        },
    },
]


# ---------------------------------------------------------------------
# WSGI
# ---------------------------------------------------------------------
WSGI_APPLICATION = "inventory_backend.wsgi.application"

# ---------------------------------------------------------------------
# REDIS / MEMURAI CACHE
# ---------------------------------------------------------------------

REDIS_URL = os.environ.get(
    "REDIS_URL",
    "redis://127.0.0.1:6379/1",
)

CACHES = {
    "default": {
        "BACKEND": (
            "django.core.cache.backends.redis.RedisCache"
        ),
        "LOCATION": REDIS_URL,
        "TIMEOUT": 300,
        "KEY_PREFIX": "ipms",
    }
}
# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": os.environ.get(
            "DATABASE_ENGINE",
            "django.db.backends.mysql",
        ),
        "NAME": os.environ.get(
            "DATABASE_NAME",
            "ipms_db1",
        ),
        "USER": os.environ.get(
            "DATABASE_USER",
            "root",
        ),
        "PASSWORD": os.environ.get(
            "DATABASE_PASSWORD",
            "root@123",
        ),
        "HOST": os.environ.get(
            "DATABASE_HOST",
            "localhost",
        ),
        "PORT": os.environ.get(
            "DATABASE_PORT",
            "3306",
        ),
        # Reuse database connections between requests in production.
        # This avoids paying the MySQL connection setup cost for every API.
        "CONN_MAX_AGE": int(
            os.environ.get(
                "DATABASE_CONN_MAX_AGE",
                "60",
            )
        ),
        "CONN_HEALTH_CHECKS": True,
    }
}

if (
    os.environ.get("DATABASE_ENGINE")
    == "django.db.backends.sqlite3"
):
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / os.environ.get(
            "DATABASE_NAME",
            "db.sqlite3",
        ),
    }


# ---------------------------------------------------------------------
# Password Validation
# ---------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "CommonPasswordValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "NumericPasswordValidator"
        )
    },
]


# ---------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------
# Static Files
# ---------------------------------------------------------------------
# Use an absolute URL prefix so Django templates render:
#     /static/images/aero360_logo.png
STATIC_URL = "/static/"

# Your source static folder shown in the project is:
#     <project-root>/static/
# Therefore Django staticfiles/finders must be told to search it.
STATICFILES_DIRS = [
    BASE_DIR / "static",
]

# collectstatic destination for deployment.
# Keep this DIFFERENT from the source static folder above.
STATIC_ROOT = BASE_DIR / "staticfiles"


# ---------------------------------------------------------------------
# Media Files
# ---------------------------------------------------------------------
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"


# ---------------------------------------------------------------------
# DRF CONFIG
# ---------------------------------------------------------------------
# Kept compatible with your current project.
# Protected views explicitly use JWTAuthentication.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.AllowAny",
    ),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": (
        "inventory_backend.pagination.OptionalPageNumberPagination"
    ),
    "PAGE_SIZE": 50,
}


# ---------------------------------------------------------------------
# CORS SETTINGS
# ---------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True


# ---------------------------------------------------------------------
# Default PK
# ---------------------------------------------------------------------
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------
# EMAIL / ZOHO SMTP
# ---------------------------------------------------------------------
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend",
)

EMAIL_HOST = os.environ.get(
    "EMAIL_HOST",
    "smtp.zoho.com",
)

EMAIL_PORT = int(
    os.environ.get(
        "EMAIL_PORT",
        "587",
    )
)

EMAIL_USE_TLS = (
    os.environ.get(
        "EMAIL_USE_TLS",
        "True",
    ).lower()
    in (
        "true",
        "1",
        "yes",
    )
)

EMAIL_USE_SSL = (
    os.environ.get(
        "EMAIL_USE_SSL",
        "False",
    ).lower()
    in (
        "true",
        "1",
        "yes",
    )
)

EMAIL_HOST_USER = os.environ.get(
    "EMAIL_HOST_USER",
    "",
)

EMAIL_HOST_PASSWORD = os.environ.get(
    "EMAIL_HOST_PASSWORD",
    "",
)

EMAIL_TIMEOUT = int(
    os.environ.get(
        "EMAIL_TIMEOUT",
        "20",
    )
)

DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL",
    EMAIL_HOST_USER,
)

IPMS_BASE_URL = os.environ.get(
    "IPMS_BASE_URL",
    "http://localhost:5173",
)


# ---------------------------------------------------------------------
# LOGIN OTP POLICY
# ---------------------------------------------------------------------
LOGIN_OTP_EXPIRY_MINUTES = int(
    os.environ.get(
        "LOGIN_OTP_EXPIRY_MINUTES",
        "5",
    )
)

LOGIN_OTP_MAX_ATTEMPTS = int(
    os.environ.get(
        "LOGIN_OTP_MAX_ATTEMPTS",
        "5",
    )
)

LOGIN_OTP_RESEND_COOLDOWN_SECONDS = int(
    os.environ.get(
        "LOGIN_OTP_RESEND_COOLDOWN_SECONDS",
        "60",
    )
)


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
# Only the dedicated API performance logger is configured here.
# Existing Django logging remains enabled.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "api_performance": {
            "format": (
                "%(asctime)s "
                "[%(levelname)s] "
                "%(message)s"
            ),
        },
    },
    "handlers": {
        "api_performance_console": {
            "class": "logging.StreamHandler",
            "formatter": "api_performance",
        },
    },
    "loggers": {
        "api.performance": {
            "handlers": [
                "api_performance_console",
            ],
            "level": "INFO",
            "propagate": False,
        },
    },
}
