"""Read-only SQL authority, context and authenticated opaque pagination."""

import base64
import hashlib
import json
from datetime import datetime, time, timedelta
from datetime import timezone as datetime_timezone
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import connection
from django.utils import timezone

from nurse.expiry_calculation import CALCULATION_VERSION, CLASSIFICATION_VERSION
from nurse.expiry_models import ExpiryAccountState

PARIS = ZoneInfo("Europe/Paris")
SOURCES = (
    "auth_user",
    "nurse_prescription",
    "nurse_patient",
    "nurse_nurse",
    "nurse_nurse_patients",
    "nurse_prescriptiondocumentversion",
    "nurse_prescriptiondocumentasset",
    "nurse_documentimport",
)
ERRORS = {
    "invalid_request": (400, "Paramètres invalides."),
    "invalid_cursor": (400, "Référence de pagination invalide."),
    "authentication_required": (401, "Authentification requise."),
    "not_found": (404, "Ressource indisponible."),
    "method_not_allowed": (405, "Méthode non autorisée."),
    "pagination_invalidated": (
        409,
        "Les informations doivent être vérifiées à nouveau.",
    ),
    "unsupported_media_type": (415, "Format non pris en charge."),
    "rate_limited": (429, "Trop de demandes."),
    "internal_error": (500, "Erreur interne."),
    "service_unavailable": (503, "Service indisponible."),
}


class ExpiryError(Exception):
    def __init__(self, code, fields=None):
        self.code = code
        self.status, message = ERRORS[code]
        self.body = {"code": code, "message": message, "fields": fields or {}}
        super().__init__(code)


def verify_guards():
    vendor = connection.vendor
    events = ("insert", "update", "delete")
    if vendor == "postgresql":
        events += ("truncate",)
    expected = {
        ("expiry_" + table.removeprefix("nurse_") + "_" + event, table, event)
        for table in SOURCES
        for event in events
    }
    with connection.cursor() as cursor:
        if vendor == "sqlite":
            cursor.execute("PRAGMA read_uncommitted")
            if cursor.fetchone()[0]:
                raise ExpiryError("service_unavailable")
            cursor.execute(
                "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"
            )
            present = {
                (name, table, event)
                for name, table, sql in cursor.fetchall()
                for event in events
                if f"AFTER {event} ON {table}".upper() in sql.upper()
            }
        elif vendor == "postgresql":
            cursor.execute("SHOW transaction_isolation")
            if cursor.fetchone()[0] != "read committed":
                raise ExpiryError("service_unavailable")
            cursor.execute("SHOW session_replication_role")
            if cursor.fetchone()[0] != "origin":
                raise ExpiryError("service_unavailable")
            cursor.execute(
                "SELECT t.tgname,c.relname,t.tgtype FROM pg_trigger t "
                "JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE NOT t.tgisinternal AND t.tgenabled IN ('O','A') "
                "AND n.nspname=current_schema() AND t.tgqual IS NULL "
                "AND t.tgattr::text=''"
            )
            event_types = {5: "insert", 17: "update", 9: "delete", 32: "truncate"}
            present = {
                (name, table, event_types.get(kind))
                for name, table, kind in cursor.fetchall()
            }
        else:
            raise ExpiryError("service_unavailable")
    if not expected <= present:
        raise ExpiryError("service_unavailable")


def authority(user_id):
    verify_guards()
    state = (
        ExpiryAccountState.objects.filter(user_id=user_id)
        .values_list("incarnation", "scope_revision", "mutation_epoch", "generation")
        .first()
    )
    if state is None:
        raise ExpiryError("service_unavailable")
    return tuple(str(value) for value in state)


def instant(value):
    return value.astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z")


def read_context(started, state):
    day = started.astimezone(PARIS).date()
    midnight = datetime.combine(day + timedelta(days=1), time.min, PARIS)
    next_day = midnight.astimezone(datetime_timezone.utc)
    versioned = [*state, day.isoformat(), CALCULATION_VERSION, CLASSIFICATION_VERSION]
    return {
        "business_date": day.isoformat(),
        "timezone": "Europe/Paris",
        "validated_at": instant(started),
        "next_day_at": instant(next_day),
        "revalidate_in_ms": max(0, int((next_day - started).total_seconds() * 1000)),
        "scope_revision": state[1],
        "collection_revision": hashlib.sha256(
            json.dumps(versioned).encode()
        ).hexdigest(),
        "calculation_version": CALCULATION_VERSION,
        "classification_version": CLASSIFICATION_VERSION,
        "mutation_epoch": state[2],
    }


def finish_read(user_id, state, context):
    if authority(user_id) != state:
        raise ExpiryError("pagination_invalidated")
    finished = timezone.now()
    if finished.astimezone(PARIS).date().isoformat() != context["business_date"]:
        raise ExpiryError("pagination_invalidated")
    context["validated_at"] = instant(finished)


def _cipher():
    key = hashlib.sha256(b"expiry-v4-cursor\0" + settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def cursor_context(user_id, query, context):
    return {
        "account": user_id,
        "filter": query["filter"],
        "ordering": query["ordering"],
        "page_size": query["page_size"],
        "context": {
            k: v
            for k, v in context.items()
            if k not in ("validated_at", "next_day_at", "revalidate_in_ms")
        },
    }


def cursor_offset(raw, expected):
    if not raw:
        return 0
    try:
        payload = json.loads(_cipher().decrypt(raw.encode("ascii")))
        if set(payload) != {"bound", "offset"}:
            raise ValueError
        bound, offset = payload["bound"], payload["offset"]
        if not isinstance(bound, dict) or bound.get("account") != expected["account"]:
            raise ValueError
        if type(offset) is not int or not 1 <= offset <= 9007199254740991:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, InvalidToken, KeyError):
        raise ExpiryError("invalid_cursor") from None
    if bound != expected:
        raise ExpiryError("pagination_invalidated")
    return offset


def next_cursor(bound, offset):
    payload = json.dumps({"bound": bound, "offset": offset}, separators=(",", ":"))
    encoded = _cipher().encrypt(payload.encode()).decode("ascii")
    if len(encoded) > 2048:
        raise ExpiryError("service_unavailable")
    return encoded
