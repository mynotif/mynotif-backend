from django.conf import settings
from rest_framework.exceptions import APIException


class FileUnavailable(APIException):
    status_code = 503
    default_detail = {
        "code": "file_temporarily_unavailable",
        "message": "Fichier temporairement indisponible.",
    }
    default_code = "file_temporarily_unavailable"


def signed_file_url(field_file):
    ttl = int(getattr(settings, "PRIVATE_FILE_URL_TTL_SECONDS", 300))
    if not 1 <= ttl <= 900:
        raise RuntimeError("PRIVATE_FILE_URL_TTL_SECONDS must be between 1 and 900")
    if not field_file:
        return ""
    try:
        return field_file.storage.url(field_file.name, expire=ttl)
    except Exception as exc:
        raise FileUnavailable() from exc


def file_exists(field_file):
    if not field_file:
        return False
    try:
        return field_file.storage.exists(field_file.name)
    except Exception as exc:
        raise FileUnavailable() from exc
