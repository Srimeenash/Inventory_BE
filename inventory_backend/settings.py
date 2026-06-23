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
load_dotenv(BASE_DIR / '.env')

# ---------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-p@%b#wzwaa^!uu_%*)&)1@+3-#zlzcbig^0)7ba4!v768ihs=a'
)

DEBUG = os.environ.get('DJANGO_DEBUG', 'True').lower() in ('true', '1', 'yes')

ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '*').split(',')

# ---------------------------------------------------------------------
# Custom User Model
# ---------------------------------------------------------------------

AUTH_USER_MODEL = 'users.User'
# ---------------------------------------------------------------------
# Installed Applications
# ---------------------------------------------------------------------
INSTALLED_APPS = [
    # Django Apps
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',   # ✅ ONLY ONCE (FIXED)
    'django_filters',
    'dashboard',
    'projects',
    'vendors',
    'components',
    'inventory',
    'procurement',
    'bom',
    'finance',
    'users',
    'roles',
    'reports',
    'notifications',
    'componentusage',
    'approvals',
    'materialrequest',
]

# ---------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',  # MUST BE TOP
    'django.middleware.security.SecurityMiddleware',

    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',

    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# ---------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------
ROOT_URLCONF = 'inventory_backend.urls'

# ---------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# WSGI
# ---------------------------------------------------------------------
WSGI_APPLICATION = 'inventory_backend.wsgi.application'

# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------
DATABASES = {
    'default': {
        'ENGINE': os.environ.get('DATABASE_ENGINE', 'django.db.backends.mysql'),
        'NAME': os.environ.get('DATABASE_NAME', 'ipms_db'),
        'USER': os.environ.get('DATABASE_USER', 'root'),
        'PASSWORD': os.environ.get('DATABASE_PASSWORD', 'root@123'),
        'HOST': os.environ.get('DATABASE_HOST', 'localhost'),
        'PORT': os.environ.get('DATABASE_PORT', '3306'),
    }
}

if os.environ.get('DATABASE_ENGINE') == 'django.db.backends.sqlite3':
    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / os.environ.get('DATABASE_NAME', 'db.sqlite3'),
    }

# ---------------------------------------------------------------------
# Password Validation
# ---------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ---------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------
# Static Files
# ---------------------------------------------------------------------
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# ---------------------------------------------------------------------
# Media Files
# ---------------------------------------------------------------------
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# ---------------------------------------------------------------------
# DRF CONFIG
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# DRF CONFIG
# ---------------------------------------------------------------------

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.AllowAny',
    ),


    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}



# ---------------------------------------------------------------------
# CORS SETTINGS
# ---------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

# ---------------------------------------------------------------------
# Default PK
# ---------------------------------------------------------------------
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

