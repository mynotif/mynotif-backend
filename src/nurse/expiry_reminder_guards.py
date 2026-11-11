"""Reservation writer coverage and serialization, independent of commands."""

from django.db import connection

from nurse.expiry_command_guards import (
    AUTHORITY_LOCK,
    COMMAND_SOURCES,
    verify_command_guards,
    verify_renewal_guards,
)
from nurse.expiry_state import ExpiryError

SOURCES = (
    *COMMAND_SOURCES,
    "nurse_expiryrenewal",
    "nurse_expiryaccountstate",
    "nurse_expiryfutureauthority",
    "nurse_pushbinding",
    "nurse_pushdestination",
    "nurse_pushregistrystate",
    "nurse_expiryreminderauthority",
    "nurse_expiryreminderpreference",
)
INVALIDATION_SOURCES = (
    "nurse_expiryfutureauthority",
    "nurse_expiryaccountstate",
    "nurse_expiryreminderpreference",
    "nurse_pushbinding",
    "nurse_pushdestination",
    "nurse_pushregistrystate",
)
EVENTS = ("insert", "update", "delete")


def expected_guards(postgres):
    events = (*EVENTS, "truncate") if postgres else EVENTS
    return (
        {
            (f"reminder_barrier_{table}_{event}", table, event, "before")
            for table in SOURCES
            for event in events
        }
        | {
            (f"reminder_invalidate_{table}_{event}", table, event, "after")
            for table in INVALIDATION_SOURCES
            for event in events
        }
        | {
            (
                "reminder_transition",
                "nurse_expiryreminderopportunity",
                "update",
                "after",
            )
        }
    )


def verify_reminder_guards():
    verify_command_guards()
    verify_renewal_guards()
    postgres = connection.vendor == "postgresql"
    with connection.cursor() as cursor:
        if postgres:
            cursor.execute(
                "SELECT t.tgname,c.relname,t.tgtype,p.proname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid=t.tgrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_proc p ON p.oid=t.tgfoid "
                "WHERE NOT t.tgisinternal AND t.tgenabled IN ('O','A') "
                "AND n.nspname=current_schema() AND t.tgqual IS NULL "
                "AND t.tgattr::text=''"
            )
            kinds = {
                6: ("insert", "before"),
                18: ("update", "before"),
                10: ("delete", "before"),
                34: ("truncate", "before"),
                5: ("insert", "after"),
                17: ("update", "after"),
                9: ("delete", "after"),
                32: ("truncate", "after"),
            }
            present = {
                (name, table, *kinds[kind])
                for name, table, kind, function in cursor.fetchall()
                if kind in kinds
                and function
                == ("reminder_writer_barrier" if kinds[kind][1] == "before" else name)
            }
        else:
            cursor.execute(
                "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"
            )
            present = {
                (name, table, event, timing)
                for name, table, sql in cursor.fetchall()
                for event in EVENTS
                for timing in ("before", "after")
                if f"{timing} {event} ON {table}".upper() in sql.upper()
            }
    if not expected_guards(postgres) <= present:
        raise ExpiryError("service_unavailable")


def acquire_reminder_authority():
    verify_reminder_guards()
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [AUTHORITY_LOCK])
            if not cursor.fetchone()[0]:
                raise ExpiryError("service_unavailable")
        else:
            cursor.execute(
                "UPDATE nurse_expiryreminderauthority "
                "SET accepting=accepting WHERE id=1"
            )
