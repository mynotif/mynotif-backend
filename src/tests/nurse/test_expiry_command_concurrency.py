"""#281 AC2–5: real PostgreSQL commits, failures and lock order.

Every worker has its own connection; Event handshakes prove overlap. No sleep,
mocked ORM lock or SQLite result is presented as PostgreSQL evidence.
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core.management.base import CommandError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.utils import timezone

from main.models import AuthSession
from tests.nurse import test_expiry_commands as commands
from tests.nurse.test_expiry_command_retention import maintenance
from tests.nurse.test_expiry_commands import (
    COMMANDS,
    FENCE,
    admit,
    assert_command,
    command_model,
    correction,
    execute,
)
from tests.nurse.test_expiry_reads_v4 import ExpiryAccount, assert_error

expiry_settings = commands.expiry_settings
command_settings = commands.command_settings
command_setup = commands.command_setup
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="Real PostgreSQL two-connection command races",
    ),
]


def on_connection(action):
    close_old_connections()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '8s'")
            cursor.execute("SET lock_timeout = '3s'")
        return action()
    finally:
        connection.close()


def test_rm3_two_idel_admissions_reserve_one_global_object(command_setup):
    h, rx = command_setup()
    other = ExpiryAccount()
    other.nurse.patients.add(h.patient)
    first = correction(h, rx)
    second = {**first, "mutation_epoch": other.page()["context"]["mutation_epoch"]}
    start = threading.Barrier(2)

    def post(account, intent):
        start.wait(timeout=5)
        return account.client.post(
            COMMANDS, {"kind": "date_correction", "intent": intent}, format="json"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(on_connection, lambda: post(h, first)),
            pool.submit(on_connection, lambda: post(other, second)),
        ]
        responses = [future.result(timeout=12) for future in futures]
    assert sum(response.status_code == 201 for response in responses) == 1
    loser = next(response for response in responses if response.status_code != 201)
    assert loser.status_code in (409, 503)
    assert_error(
        loser,
        loser.status_code,
        "reconciliation_required"
        if loser.status_code == 409
        else "service_unavailable",
    )
    assert command_model().objects.count() == 1


def test_rm4_same_command_concurrent_puts_have_at_most_one_effect(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    start = threading.Barrier(2)

    def put():
        start.wait(timeout=5)
        return execute(h, pending, intent)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [
            future.result(timeout=12)
            for future in [
                pool.submit(on_connection, put),
                pool.submit(on_connection, put),
            ]
        ]
    assert any(response.status_code == 200 for response in responses)
    terminal = assert_command(execute(h, pending, intent), 200, "succeeded")
    for response in responses:
        if response.status_code == 200:
            assert assert_command(response, 200, "succeeded") == terminal
        else:
            assert response.status_code in (202, 503)
    assert (
        command_model("ExpiryCorrectionAudit")
        .objects.filter(command_id=pending["command_id"])
        .count()
        == 1
    )


@pytest.mark.parametrize("writer", ["session", "association", "password"])
def test_k19_revocation_committed_after_http_auth_prevents_execution(
    command_setup, writer
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    reached, resume = threading.Event(), threading.Event()
    before = (rx.start_date, rx.end_date)

    def request():
        paused = False
        authority_seen = []

        def pause_auth(execute_sql, sql, params, many, context):
            nonlocal paused
            result = execute_sql(sql, params, many, context)
            if "pg_try_advisory_xact_lock" in sql.lower():
                authority_seen.append(sql)
            if not paused and re.search(
                r'^\s*SELECT\b.*\bFROM\s+"?main_authtoken\b', sql, re.I | re.S
            ):
                assert not authority_seen, "Pause must precede the command authority"
                paused = True
                reached.set()
                assert resume.wait(6), "Revocation did not release the HTTP auth read"
            return result

        with connection.execute_wrapper(pause_auth):
            return execute(h, pending, intent)

    changes = {
        "session": lambda: AuthSession.objects.filter(pk=h.pair["session_id"]).update(
            revoked_at=timezone.now()
        ),
        "association": lambda: h.nurse.patients.remove(h.patient),
        "password": lambda: User.objects.filter(pk=h.user.pk).update(
            password="synthetic-changed"
        ),
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, request)
        try:
            assert reached.wait(6), "HTTP authentication did not read its session"
            changes[writer]()  # autocommit must finish before the PUT resumes
        finally:
            resume.set()
        response = future.result(timeout=12)
    assert_error(
        response,
        404 if writer == "association" else 401,
        "not_found" if writer == "association" else "authentication_required",
    )
    commands.assert_unchanged(rx, before)
    assert (
        not command_model("ExpiryCorrectionAudit")
        .objects.filter(command_id=pending["command_id"])
        .exists()
    )


@pytest.mark.parametrize("point", ["clinical", "terminal"])
def test_rm4_database_fault_rolls_back_effect_result_audit_and_future_watermark(
    command_setup, point
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    before = (rx.start_date, rx.end_date)
    before_context = h.page()["context"]
    future_model = command_model("ExpiryFutureAuthority")
    before_futures = list(future_model.objects.order_by("pk").values())
    hit = []

    def fail_after_write(execute_sql, sql, params, many, context):
        result = execute_sql(sql, params, many, context)
        clinical = re.search(r'^\s*UPDATE\s+"?nurse_prescription"?\s', sql, re.I)
        terminal = re.search(
            r'^\s*UPDATE\s+"?nurse_expirycommand"?\s', sql, re.I
        ) and "succeeded" in (params or ())
        if not hit and {"clinical": clinical, "terminal": terminal}[point]:
            hit.append(point)
            raise DatabaseError("synthetic rollback probe")
        return result

    h.client.raise_request_exception = False
    with connection.execute_wrapper(fail_after_write):
        response = execute(h, pending, intent)
    assert hit == [point], "Fault must happen after the requested durable write"
    assert response.status_code in (500, 503)
    commands.assert_unchanged(rx, before)
    assert h.page()["context"] == before_context
    row = command_model().objects.get(pk=pending["command_id"])
    assert row.state == "pending" and row.result is None and row.terminal_at is None
    assert list(future_model.objects.order_by("pk").values()) == before_futures
    assert (
        not command_model("ExpiryCorrectionAudit")
        .objects.filter(command_id=pending["command_id"])
        .exists()
    )
    assert_command(execute(h, pending, intent), 200, "succeeded")


def test_b01_prelocked_user_and_command_advisory_never_deadlock_or_false_ack(
    command_setup,
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    user_locked, advisory_attempted = threading.Event(), threading.Event()

    def revoke_with_b01_lock_order():
        try:
            with transaction.atomic():
                User.objects.select_for_update().get(pk=h.user.pk)
                user_locked.set()
                assert advisory_attempted.wait(6), (
                    "Command never reached shared authority"
                )
                AuthSession.objects.filter(pk=h.pair["session_id"]).update(
                    revoked_at=timezone.now()
                )
            return "committed"
        except DatabaseError:
            return "rolled_back"

    def put():
        def observe_advisory(execute_sql, sql, params, many, context):
            result = execute_sql(sql, params, many, context)
            if "pg_try_advisory_xact_lock" in sql.lower():
                advisory_attempted.set()
            return result

        with connection.execute_wrapper(observe_advisory):
            return execute(h, pending, intent)

    with ThreadPoolExecutor(max_workers=2) as pool:
        revoke = pool.submit(on_connection, revoke_with_b01_lock_order)
        assert user_locked.wait(6), "B01 writer did not lock User first"
        put_future = pool.submit(on_connection, put)
        outcome = revoke.result(timeout=12)
        response = put_future.result(timeout=12)
    assert advisory_attempted.is_set()
    if outcome == "committed":
        assert response.status_code in (401, 503)
        assert (
            command_model().objects.get(pk=pending["command_id"]).state != "succeeded"
        )
    else:
        assert AuthSession.objects.get(pk=h.pair["session_id"]).revoked_at is None
        assert response.status_code in (200, 503)


def test_fence_racing_engaged_put_never_leaves_closed_epoch_executable(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    reached, resume = threading.Event(), threading.Event()

    def put():
        def pause_write(execute_sql, sql, params, many, context):
            if not reached.is_set() and re.search(
                r'^\s*UPDATE\s+"?nurse_prescription"?\s', sql, re.I
            ):
                reached.set()
                assert resume.wait(6), "Concurrent fence did not finish"
            return execute_sql(sql, params, many, context)

        with connection.execute_wrapper(pause_write):
            return execute(h, pending, intent)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, put)
        try:
            assert reached.wait(6), "Command did not engage its clinical transaction"
            fence = h.client.post(FENCE, {}, format="json")
            assert (
                fence.status_code == 503
            )  # try-lock closes concurrent authority access
        finally:
            resume.set()
        result = assert_command(future.result(timeout=12), 200, "succeeded")
    assert h.client.post(FENCE, {}, format="json").status_code == 200
    assert assert_command(execute(h, pending, intent), 200, "succeeded") == result
    assert_error(
        h.client.post(
            COMMANDS, {"kind": "date_correction", "intent": intent}, format="json"
        ),
        409,
        "command_epoch_closed",
    )


def test_truncate_waiting_on_engaged_command_is_bounded_and_has_no_false_commit(
    command_setup,
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    reached, resume = threading.Event(), threading.Event()

    def put():
        def pause_write(execute_sql, sql, params, many, context):
            if not reached.is_set() and re.search(
                r'^\s*UPDATE\s+"?nurse_prescription"?\s', sql, re.I
            ):
                reached.set()
                assert resume.wait(6), "TRUNCATE did not finish within its lock timeout"
            return execute_sql(sql, params, many, context)

        with connection.execute_wrapper(pause_write):
            return execute(h, pending, intent)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, put)
        try:
            assert reached.wait(6)
            with pytest.raises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '1s'")
                    cursor.execute("TRUNCATE nurse_prescription CASCADE")
        finally:
            resume.set()
        assert_command(future.result(timeout=12), 200, "succeeded")
    assert h.detail(rx).status_code == 200


def test_rm6_purge_cannot_assume_failure_of_a_pending_locked_elsewhere(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    terminal = admit(h, intent)
    assert_command(execute(h, terminal, intent), 200, "succeeded")
    old = timezone.now() - timedelta(days=31)
    command_model().objects.filter(pk=terminal["command_id"]).update(
        recorded_at=old - timedelta(hours=1), terminal_at=old
    )
    pending_rx = h.prescription()
    pending_intent = correction(h, pending_rx)
    pending = admit(h, pending_intent)
    locked, release = threading.Event(), threading.Event()

    def hold_pending():
        with transaction.atomic():
            command_model().objects.select_for_update().get(pk=pending["command_id"])
            locked.set()
            assert release.wait(6), "Purge did not release the locked pending"

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, hold_pending)
        try:
            assert locked.wait(6)
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '1s'")
            try:
                maintenance("purge_expiry_commands")
            except (DatabaseError, CommandError):
                pass  # Closed CLI failure is permitted; a presumed terminal is not.
            row = command_model().objects.get(pk=pending["command_id"])
            assert row.state == "pending" and row.terminal_at is None
            assert row.payload_ciphertext
            assert command_model().objects.filter(pk=terminal["command_id"]).exists()
        finally:
            release.set()
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '0'")
        future.result(timeout=12)
    maintenance("purge_expiry_commands")
    result = assert_command(
        h.client.get(f"{COMMANDS}{pending['command_id']}/"), 200, "rejected"
    )
    assert result["error"]["code"] == "command_abandoned"
    commands.assert_unchanged(pending_rx, (pending_rx.start_date, pending_rx.end_date))


def test_ac7_postgres_session_guard_is_required_and_disabled_guard_closes_commands(
    expiry_settings, settings
):
    from django.db import transaction

    settings.EXPIRY_COMMANDS_ENABLED = True
    settings.EXPIRY_COMMAND_NAMESPACE = commands.NAMESPACE
    settings.EXPIRY_COMMAND_ACTIVE_KEY = "v1"
    settings.EXPIRY_COMMAND_KEYS = commands.KEYS
    commands.open_runtime()
    account = ExpiryAccount()
    rx = account.prescription()
    intent = commands.correction(account, rx)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "WHERE c.relname='main_authsession' AND NOT t.tgisinternal "
            "AND (t.tgtype & 1)=0 AND (t.tgtype & 2)=2"
        )
        guards = [row[0] for row in cursor.fetchall()]
    assert guards, "B01 AuthSession needs the shared BEFORE STATEMENT authority barrier"
    with transaction.atomic():
        with connection.cursor() as cursor:
            for name in guards:
                cursor.execute(
                    "ALTER TABLE main_authsession DISABLE TRIGGER "
                    + connection.ops.quote_name(name)
                )
        assert account.detail(rx).json()["can_correct_dates"] is False
        assert_error(
            account.client.post(
                commands.COMMANDS,
                {"kind": "date_correction", "intent": intent},
                format="json",
            ),
            503,
            "service_unavailable",
        )
        transaction.set_rollback(True)
    assert account.detail(rx).json()["can_correct_dates"] is True


@pytest.mark.parametrize("field", ["token_hash", "session_id", "kind"])
def test_presented_token_is_reverified_after_http_authentication(command_setup, field):
    from main.models import AuthToken

    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    _, second_pair = commands.new_client(h)
    if field == "kind":
        AuthToken.objects.filter(
            session_id=h.pair["session_id"], kind="refresh"
        ).delete()
    elif field == "session_id":
        AuthToken.objects.filter(session_id=second_pair["session_id"]).delete()
    reached, resume = threading.Event(), threading.Event()
    before = (rx.start_date, rx.end_date)

    def request():
        def pause_auth(execute_sql, sql, params, many, context):
            result = execute_sql(sql, params, many, context)
            if not reached.is_set() and re.search(
                r'^\s*SELECT\b.*\bFROM\s+"?main_authtoken\b', sql, re.I | re.S
            ):
                reached.set()
                assert resume.wait(6)
            return result

        with connection.execute_wrapper(pause_auth):
            return execute(h, pending, intent)

    changes = {
        "token_hash": "invalidated-hash",
        "session_id": second_pair["session_id"],
        "kind": "refresh",
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, request)
        try:
            assert reached.wait(6)
            AuthToken.objects.filter(pk=h.pair["access_token"].split(".")[0]).update(
                **{field: changes[field]}
            )
        finally:
            resume.set()
        response = future.result(timeout=12)
    assert_error(response, 401, "authentication_required")
    commands.assert_unchanged(rx, before)
    assert command_model().objects.get(pk=pending["command_id"]).state == "pending"
    assert not command_model("ExpiryCorrectionAudit").objects.exists()


def test_sql_authority_closure_cannot_acknowledge_while_command_is_engaged(
    command_setup,
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    reached, resume = threading.Event(), threading.Event()

    def put():
        def pause_write(execute_sql, sql, params, many, context):
            if not reached.is_set() and re.search(
                r'^\s*UPDATE\s+"?nurse_prescription"?\s', sql, re.I
            ):
                reached.set()
                assert resume.wait(6)
            return execute_sql(sql, params, many, context)

        with connection.execute_wrapper(pause_write):
            return execute(h, pending, intent)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, put)
        try:
            assert reached.wait(6)
            with pytest.raises(DatabaseError), transaction.atomic():
                command_model("ExpiryCommandAuthority").objects.update(accepting=False)
            assert (
                command_model("ExpiryCommandAuthority").objects.get(pk=1).accepting
                is True
            )
        finally:
            resume.set()
        assert_command(future.result(timeout=12), 200, "succeeded")
    command_model("ExpiryCommandAuthority").objects.update(accepting=False)
    assert h.detail(rx).json()["can_correct_dates"] is False
