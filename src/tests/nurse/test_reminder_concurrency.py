"""#283 AC3: actual PostgreSQL connections and deterministic overlap."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from django.db import DatabaseError, connection, transaction

from nurse.expiry_state import ExpiryError
from tests.nurse import test_reminder_reservations as reminders
from tests.nurse.test_expiry_command_concurrency import on_connection
from tests.nurse.test_expiry_commands import command_model

expiry_settings = reminders.expiry_settings
reminder_setup = reminders.reminder_setup
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="Real PostgreSQL transactions required",
    ),
]


def test_ac3_two_workers_commit_one_daily_event_with_distinct_connections(
    reminder_setup,
):
    account, _, _, route, pref = reminder_setup()
    start = threading.Barrier(2)

    def worker():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pid = cursor.fetchone()[0]
        start.wait(timeout=5)
        try:
            result = reminders.reserve(account, pref, [route])
        except ExpiryError as exc:
            assert exc.code == "service_unavailable"
            result = None
        return pid, result

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(on_connection, worker) for _ in range(2)]
        outcomes = [future.result(timeout=15) for future in futures]
    assert len({pid for pid, _ in outcomes}) == 2
    result = reminders.reserve(account, pref, [route])
    reminders.assert_reserved(result)
    assert any(value == result for _, value in outcomes)
    assert all(value is None or value == result for _, value in outcomes)
    assert reminders.model("Event").objects.count() == 1
    assert reminders.model("EventItem").objects.count() == 1


def test_ac3_sql_writer_is_blocked_even_when_commands_are_closed(reminder_setup):
    account, rx, _, route, pref = reminder_setup()
    command_model("ExpiryCommandAuthority").objects.update(accepting=False)
    reached, resume = threading.Event(), threading.Event()
    table = reminders.model("Event")._meta.db_table.upper()

    def reserve():
        def pause(execute, sql, params, many, context):
            if not reached.is_set() and sql.lstrip().upper().startswith(
                f'INSERT INTO "{table}"'
            ):
                reached.set()
                assert resume.wait(6)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(pause):
            return reminders.reserve(account, pref, [route])

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, reserve)
        try:
            assert reached.wait(6), "Reservation never reached durable admission"
            with pytest.raises(DatabaseError) as blocked, transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE nurse_prescription SET end_date=%s WHERE id=%s",
                        [date(2026, 4, 8), rx.pk],
                    )
            assert blocked.value.__cause__.pgcode == "55P03"
        finally:
            resume.set()
        reminders.assert_reserved(future.result(timeout=15))
    rx.refresh_from_db()
    assert rx.end_date == date(2026, 4, 7)


@pytest.mark.parametrize("part", ["Event", "EventItem", "EventRoute"])
def test_ac3_failure_after_any_admission_component_rolls_back_and_retry_is_unique(
    reminder_setup, part
):
    account, _, _, route, pref = reminder_setup()
    table = reminders.model(part)._meta.db_table.upper()
    hit = []

    def crash(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if not hit and sql.lstrip().upper().startswith(f'INSERT INTO "{table}"'):
            hit.append(part)
            raise DatabaseError("synthetic reservation crash")
        return result

    try:
        with connection.execute_wrapper(crash):
            reminders.reserve(account, pref, [route])
    except (DatabaseError, ExpiryError):
        pass
    assert hit == [part], "Crash seam was not exercised"
    for name in ("Event", "EventItem", "EventRoute"):
        assert not reminders.model(name).objects.exists()
    assert (
        not reminders.model("Opportunity")
        .objects.filter(admitted_at__isnull=False)
        .exists()
    )
    result = reminders.reserve(account, pref, [route])
    assert reminders.reserve(account, pref, [route]) == result
    assert reminders.model("Event").objects.count() == 1


def test_ac3_pre_authenticated_worker_revalidates_scope_before_admission(
    reminder_setup,
):
    account, _, _, route, pref = reminder_setup()
    ready, resume = threading.Event(), threading.Event()

    def worker():
        authenticated = reminders.request(account)
        assert authenticated.user.pk == account.user.pk
        ready.set()
        assert resume.wait(6)
        try:
            return reminders.services().reserve_day(
                authenticated,
                expected_preference_revision=pref["revision"],
                selected_routes=[route],
            )
        except ExpiryError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, worker)
        try:
            assert ready.wait(6)
            account.nurse.patients.remove(account.patient)
        finally:
            resume.set()
        result = future.result(timeout=15)
    assert result == "not_found" or result["state"] == "no_opportunity"
    assert not reminders.model("Event").objects.exists()
