"""
Django settings for main project.

Generated by 'django-admin startproject' using Django 3.2.5.

For more information on this file, see
https://docs.djangoproject.com/en/3.2/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/3.2/ref/settings/
"""

import json
import os
from pathlib import Path

import sentry_sdk
from django.core.management.utils import get_random_secret_key
from dotenv import load_dotenv
from sentry_sdk.integrations.django import DjangoIntegration

load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/3.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get("SECRET_KEY", get_random_secret_key())

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = not bool(json.loads(os.environ.get("PRODUCTION", "0")))

ALLOWED_HOSTS = json.loads(
    os.environ.get("ALLOWED_HOSTS", '["127.0.0.1", "localhost"]')
)
# https://docs.djangoproject.com/en/4.2/releases/4.0/#csrf-trusted-origins-changes
CSRF_TRUSTED_ORIGINS = json.loads(
    os.environ.get("CSRF_TRUSTED_ORIGINS", '["http://127.0.0.1", "http://localhost"]')
)


# Application definition

DJANGO_CORE_APP = (
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
)

THIRDPARTY_APP = (
    "corsheaders",
    "django_extensions",
    "djoser",
    "drf_spectacular",
    "rest_framework",
    "rest_framework.authtoken",
)

CUSTOM_APPS = (
    "main",
    "nurse",
    "payment",
)

INSTALLED_APPS = DJANGO_CORE_APP + THIRDPARTY_APP + CUSTOM_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "main.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [TEMPLATES_DIR],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "main.wsgi.application"


# Database
# https://docs.djangoproject.com/en/3.2/ref/settings/#databases

DEFAULT_DATABASE_ENGINE = "django.db.backends.sqlite3"
DEFAULT_DATABASE_NAME = BASE_DIR / "db.sqlite3"
DATABASE_ENGINE = os.environ.get("DATABASE_ENGINE", DEFAULT_DATABASE_ENGINE)
DATABASE_NAME = os.environ.get("DATABASE_NAME", DEFAULT_DATABASE_NAME)
DATABASE_USER = os.environ.get("DATABASE_USER")
DATABASE_PASSWORD = os.environ.get("DATABASE_PASSWORD")
DATABASE_HOST = os.environ.get("DATABASE_HOST")
DATABASE_PORT = os.environ.get("DATABASE_PORT")

DATABASES = {
    "default": {
        "ENGINE": DATABASE_ENGINE,
        "NAME": DATABASE_NAME,
        "USER": DATABASE_USER,
        "PASSWORD": DATABASE_PASSWORD,
        "HOST": DATABASE_HOST,
        "PORT": DATABASE_PORT,
    }
}


# Password validation
# https://docs.djangoproject.com/en/3.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
        ),
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/3.2/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.2/howto/static-files/

STATIC_ROOT = BASE_DIR / "staticfiles"

STATIC_URL = "/static/"

# Default primary key field type
# https://docs.djangoproject.com/en/3.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CORS_ALLOWED_ORIGINS = json.loads(os.environ.get("CORS_ALLOWED_ORIGINS", "[]"))
# Vercel & Netlify dynamic previews
CORS_ALLOWED_ORIGIN_REGEXES = json.loads(
    os.environ.get("CORS_ALLOWED_ORIGIN_REGEXES", "[]")
)

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Mynotif",
    "DESCRIPTION": "API Mynotif",
    "VERSION": "1.0.0",
}

STATICFILES_STORAGE_BACKEND = "django.contrib.staticfiles.storage.StaticFilesStorage"

# AWS s3
DEFAULT_FILE_STORAGE_BACKEND = "storages.backends.s3boto3.S3Boto3Storage"
AWS_STORAGE_BUCKET_NAME = "mynotif-prescription"
AWS_S3_REGION_NAME = "eu-west-3"
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")

STORAGES = {
    "default": {
        "BACKEND": DEFAULT_FILE_STORAGE_BACKEND,
    },
    "staticfiles": {
        "BACKEND": STATICFILES_STORAGE_BACKEND,
    },
}

# Email configuration
EMAIL_USE_TLS = bool(json.loads(os.environ.get("EMAIL_USE_TLS", "0")))
EMAIL_USE_SSL = bool(json.loads(os.environ.get("EMAIL_USE_SSL", "0")))
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = json.loads(os.environ.get("EMAIL_PORT", "25"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")

# Djoser
DJOSER = {
    "PASSWORD_RESET_CONFIRM_URL": os.environ.get("PASSWORD_RESET_CONFIRM_URL"),
}

# one signal configuration
ONESIGNAL_API_KEY = os.environ.get("ONESIGNAL_API_KEY")
ONESIGNAL_APP_ID = os.environ.get("ONESIGNAL_APP_ID")

# django-templated-mail
DOMAIN = os.environ.get("TEMPLATED_MAIL_DOMAIN", "")
SITE_NAME = os.environ.get("TEMPLATED_SITE_NAME", "")

if SENTRY_DSN := os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        # Set traces_sample_rate to 1.0 to capture 100%
        # of transactions for performance monitoring.
        # We recommend adjusting this value in production.
        traces_sample_rate=1.0,
        # If you wish to associate users to errors (assuming you are using
        # django.contrib.auth) you may enable sending PII data.
        send_default_pii=True,
    )

# Stripe
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# Frontend URL, used for the Stripe success/cancel redirect
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

# Maximum number of free patients and prescriptions
# these values need to be in sync until we implement the single source of truth
# https://github.com/mynotif/mynotif-frontend/blob/3d1c858/src/hook/patientManagement.ts#L4
# https://github.com/mynotif/mynotif-frontend/blob/3d1c858/src/hook/prescriptionManagement.ts#L4
FREE_PATIENT_LIMIT = 15
FREE_PRESCRIPTION_LIMIT = 15
