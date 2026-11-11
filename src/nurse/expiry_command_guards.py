"""Fail closed unless every writer participates in the installed authority."""

from django.db import connection

from nurse.expiry_state import SOURCES, ExpiryError, verify_guards

AUTHORITY_LOCK = 28123601
COMMAND_SOURCES = (
    *SOURCES,
    "main_authsession",
    "main_authtoken",
    "nurse_expirycommandauthority",
)
EVENTS = ("insert", "update", "delete")


def verify_command_guards():
    verify_guards()
    postgres = connection.vendor == "postgresql"
    events = (*EVENTS, "truncate") if postgres else EVENTS
    expected = {
        (f"expiry_cmd_{table}_{event}", table, event, "before")
        for table in COMMAND_SOURCES
        for event in events
    } | {
        (f"expiry_cmd_future_{event}", "nurse_expiryprescriptionstate", event, "after")
        for event in EVENTS
    }
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
            before_types = {6: "insert", 18: "update", 10: "delete", 34: "truncate"}
            after_types = {5: "insert", 17: "update", 9: "delete"}
            present = set()
            for name, table, kind, function in cursor.fetchall():
                if kind in before_types and function == "expiry_command_barrier":
                    present.add((name, table, before_types[kind], "before"))
                elif kind in after_types and function == name:
                    present.add((name, table, after_types[kind], "after"))
        elif connection.vendor == "sqlite":
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
        else:
            raise ExpiryError("service_unavailable")
    if not expected <= present:
        raise ExpiryError("service_unavailable")


def acquire_authority():
    verify_command_guards()
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [AUTHORITY_LOCK])
            if not cursor.fetchone()[0]:
                raise ExpiryError("service_unavailable")
    else:
        # Acquire SQLite's writer reservation before reading authorization/CAS.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE nurse_expirycommandauthority SET accepting=accepting WHERE id=1"
            )
