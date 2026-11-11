"""External restore namespace and keys are prerequisites, never auto-opened."""

import uuid

from django.conf import settings
from django.db import DatabaseError

from nurse.expiry_command_crypto import keys
from nurse.expiry_command_guards import verify_command_guards
from nurse.expiry_command_models import ExpiryCommandAuthority
from nurse.expiry_state import ExpiryError


def external_namespace():
    try:
        raw = settings.EXPIRY_COMMAND_NAMESPACE
        namespace = uuid.UUID(raw)
        if str(namespace) != raw or namespace.version != 4:
            raise ValueError
        return namespace
    except (AttributeError, TypeError, ValueError):
        raise ExpiryError("service_unavailable") from None


def runtime():
    if getattr(settings, "EXPIRY_COMMANDS_ENABLED", False) is not True:
        raise ExpiryError("service_unavailable")
    namespace = external_namespace()
    version = getattr(settings, "EXPIRY_COMMAND_ACTIVE_KEY", "")
    keys(version)
    verify_command_guards()
    authority = ExpiryCommandAuthority.objects.filter(pk=1).first()
    if authority is None or not authority.accepting or authority.namespace != namespace:
        raise ExpiryError("service_unavailable")
    return namespace, version


def correction_available():
    try:
        runtime()
        return True
    except (ExpiryError, DatabaseError):
        return False
