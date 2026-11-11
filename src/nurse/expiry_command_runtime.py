"""External restore namespace and keys are prerequisites, never auto-opened."""

import uuid

from django.conf import settings
from django.db import DatabaseError, connection

from nurse.expiry_command_crypto import keys
from nurse.expiry_command_guards import verify_command_guards, verify_renewal_guards
from nurse.expiry_command_models import ExpiryCommandAuthority, ExpiryRenewal
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


def runtime(kind="date_correction"):
    if getattr(settings, "EXPIRY_COMMANDS_ENABLED", False) is not True:
        raise ExpiryError("service_unavailable")
    if kind == "renewal_confirmation":
        if getattr(settings, "EXPIRY_RENEWALS_ENABLED", False) is not True:
            raise ExpiryError("service_unavailable")
    namespace = external_namespace()
    version = getattr(settings, "EXPIRY_COMMAND_ACTIVE_KEY", "")
    keys(version)
    verify_command_guards()
    # Corrections also depend on relation state. Qualify every installed writer
    # regardless of the renewal activation flag; genuinely older schemas keep
    # their parent command authority without requiring nonexistent guards.
    if (
        kind == "renewal_confirmation"
        or ExpiryRenewal._meta.db_table in connection.introspection.table_names()
    ):
        verify_renewal_guards()
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


def renewal_available():
    try:
        runtime("renewal_confirmation")
        return True
    except (ExpiryError, DatabaseError):
        return False
