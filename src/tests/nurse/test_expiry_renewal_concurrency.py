"""#282 AC3–5 / RM1,4–7: PG16 two-connection barriers, without sleeps."""

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from main.models import AuthSession
from nurse.models import Prescription
from tests.nurse import test_expiry_renewals as renewals
from tests.nurse.test_expiry_command_concurrency import on_connection
from tests.nurse.test_expiry_command_retention import maintenance
from tests.nurse.test_expiry_renewals import commands, reads

expiry_settings = renewals.expiry_settings
command_settings = renewals.command_settings
command_setup = renewals.command_setup
renewal_settings = renewals.renewal_settings
renewal_setup = renewals.renewal_setup
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="Requires real PostgreSQL 16 transactions",
    ),
]


@pytest.mark.parametrize("contender_kind", ["renewal", "correction"])
@pytest.mark.parametrize("shared", ["old", "new"])
def test_rm4_concurrent_cross_idel_admissions_reserve_both_objects(
    renewal_setup, contender_kind, shared
):
    account, old, new = renewal_setup()
    other = reads.ExpiryAccount()
    other.nurse.patients.add(account.patient)
    third = account.prescription()
    renewals.make_source(account, third)
    intent = renewals.renewal(account, old, new)
    selected = {"old": old, "new": new}[shared]
    contender_pair = {"old": (old, third), "new": (third, new)}[shared]
    other_intent = (
        renewals.renewal(account, *contender_pair)
        if contender_kind == "renewal"
        else commands.correction(account, selected)
    )
    other_intent["mutation_epoch"] = other.page()["context"]["mutation_epoch"]
    start = threading.Barrier(2)

    def first():
        start.wait(timeout=5)
        return renewals.post(account, intent)

    def second():
        start.wait(timeout=5)
        if contender_kind == "renewal":
            return renewals.post(other, other_intent)
        return other.client.post(
            commands.COMMANDS,
            {"kind": "date_correction", "intent": other_intent},
            format="json",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(on_connection, first),
            pool.submit(on_connection, second),
        ]
        responses = [future.result(timeout=12) for future in futures]
    assert sum(response.status_code == 201 for response in responses) == 1
    loser = next(response for response in responses if response.status_code != 201)
    assert loser.status_code in (409, 503)
    reads.assert_error(
        loser,
        loser.status_code,
        {409: "reconciliation_required", 503: "service_unavailable"}[loser.status_code],
    )
    assert commands.command_model().objects.count() == 1
    assert not renewals.relation_model().objects.exists()


def test_rm5_concurrent_same_command_puts_commit_one_relation(renewal_setup):
    account, old, new = renewal_setup()
    intent = renewals.renewal(account, old, new)
    pending = renewals.admit(account, intent)
    start = threading.Barrier(2)

    def put():
        start.wait(timeout=5)
        return renewals.execute(account, pending, intent)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(on_connection, put), pool.submit(on_connection, put)]
        responses = [future.result(timeout=12) for future in futures]
    assert any(response.status_code == 200 for response in responses)
    terminal = renewals.assert_command(
        renewals.execute(account, pending, intent), 200, "succeeded"
    )
    for response in responses:
        if response.status_code == 200:
            assert renewals.assert_command(response, 200, "succeeded") == terminal
        else:
            assert response.status_code in (202, 503)
    assert (
        renewals.relation_model()
        .objects.filter(command_id=pending["command_id"])
        .count()
        == 1
    )


@pytest.mark.parametrize(
    "writer", ["session", "password", "association", "source", "new_patient"]
)
def test_rm1_authority_or_source_change_after_http_auth_wins_before_commit(
    renewal_setup, writer
):
    account, old, new = renewal_setup()
    outsider = reads.ExpiryAccount()
    intent = renewals.renewal(account, old, new)
    pending = renewals.admit(account, intent)
    reached, resume = threading.Event(), threading.Event()

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
                assert not authority_seen
                paused = True
                reached.set()
                assert resume.wait(6), "Writer did not finish before authority reread"
            return result

        with connection.execute_wrapper(pause_auth):
            return renewals.execute(account, pending, intent)

    changes = {
        "session": lambda: AuthSession.objects.filter(
            pk=account.pair["session_id"]
        ).update(revoked_at=timezone.now()),
        "password": lambda: User.objects.filter(pk=account.user.pk).update(
            password="synthetic-changed"
        ),
        "association": lambda: account.nurse.patients.remove(account.patient),
        "source": lambda: renewals.make_source(account, new, number=2),
        "new_patient": lambda: Prescription.objects.filter(pk=new.pk).update(
            patient_id=outsider.patient.pk
        ),
    }

    def clinical_authorities():
        return {
            name: list(commands.command_model(name).objects.order_by("pk").values())
            for name in (
                "ExpiryPrescriptionState",
                "ExpiryFutureAuthority",
                "ExpiryAccountState",
            )
        }

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, request)
        try:
            assert reached.wait(6), "Authentication seam was not exercised"
            changes[writer]()
            clinical_before = list(Prescription.objects.order_by("pk").values())
            authorities_before = clinical_authorities()
        finally:
            resume.set()
        response = future.result(timeout=12)
    if writer == "source":
        terminal = renewals.assert_command(response, 200, "rejected")
        assert terminal["error"]["code"] == "revision_conflict"
    else:
        code = (
            "not_found"
            if writer in ("association", "new_patient")
            else "authentication_required"
        )
        reads.assert_error(response, 404 if code == "not_found" else 401, code)
    assert not renewals.relation_model().objects.exists()
    assert list(Prescription.objects.order_by("pk").values()) == clinical_before
    assert clinical_authorities() == authorities_before
    assert account.detail(old).status_code in (200, 401, 404)


@pytest.mark.parametrize("point", ["relation", "terminal"])
def test_rm5_fault_after_durable_write_rolls_back_every_component(renewal_setup, point):
    account, old, new = renewal_setup()
    intent = renewals.renewal(account, old, new)
    pending = renewals.admit(account, intent)
    before = renewals.snapshot(account, old, new)
    relations_before = list(renewals.relation_model().objects.values())
    hit = []
    relation_table = renewals.relation_model()._meta.db_table

    def fail_after_write(execute_sql, sql, params, many, context):
        result = execute_sql(sql, params, many, context)
        statement = sql.lstrip().upper()
        values = tuple(params or ())
        matches = {
            "relation": statement.startswith(f'INSERT INTO "{relation_table.upper()}"'),
            "terminal": bool(
                re.search(r'^\s*UPDATE\s+"?nurse_expirycommand"?\s', sql, re.I)
            )
            and "succeeded" in values,
        }
        if not hit and matches[point]:
            hit.append(point)
            raise DatabaseError("synthetic renewal rollback probe")
        return result

    account.client.raise_request_exception = False
    with connection.execute_wrapper(fail_after_write):
        response = renewals.execute(account, pending, intent)
    assert hit == [point], f"Fault seam {point} was not exercised"
    assert response.status_code in (500, 503)
    assert renewals.snapshot(account, old, new) == before
    assert list(renewals.relation_model().objects.values()) == relations_before
    row = commands.command_model().objects.get(pk=pending["command_id"])
    assert row.state == "pending" and row.terminal_at is None
    assert row.result is row.error is None and row.payload_ciphertext
    renewals.assert_command(
        renewals.execute(account, pending, intent), 200, "succeeded"
    )


@pytest.mark.parametrize("winner", ["correction", "renewal"])
def test_rm6_engaged_correction_and_renewal_cannot_merge_stale_human_decisions(
    renewal_setup, winner
):
    account, old, new = renewal_setup()
    renewal_intent = renewals.renewal(account, old, new)
    correction_intent = commands.correction(account, new)
    admission = {
        "correction": lambda: commands.admit(account, correction_intent),
        "renewal": lambda: renewals.admit(account, renewal_intent),
    }
    execute_winner = {
        "correction": lambda pending: commands.execute(
            account, pending, correction_intent
        ),
        "renewal": lambda pending: renewals.execute(account, pending, renewal_intent),
    }
    pending = admission[winner]()
    reached, resume = threading.Event(), threading.Event()

    def request():
        def pause_write(execute_sql, sql, params, many, context):
            if not reached.is_set() and sql.lstrip().upper().startswith(
                (
                    'UPDATE "NURSE_PRESCRIPTION" ',
                    'INSERT INTO "NURSE_EXPIRYRENEWAL"',
                )
            ):
                reached.set()
                assert resume.wait(6)
            return execute_sql(sql, params, many, context)

        with connection.execute_wrapper(pause_write):
            return execute_winner[winner](pending)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, request)
        try:
            assert reached.wait(6), "Winner never engaged its durable effect"
            response = (
                renewals.post(account, renewal_intent)
                if winner == "correction"
                else account.client.post(
                    commands.COMMANDS,
                    {"kind": "date_correction", "intent": correction_intent},
                    format="json",
                )
            )
            reads.assert_error(response, 503, "service_unavailable")
        finally:
            resume.set()
        terminal_response = future.result(timeout=12)
    if winner == "correction":
        commands.assert_command(terminal_response, 200, "succeeded")
        loser = renewals.admit(account, renewal_intent)
        terminal = renewals.assert_command(
            renewals.execute(account, loser, renewal_intent), 200, "rejected"
        )
    else:
        renewals.assert_command(terminal_response, 200, "succeeded")
        loser = commands.admit(account, correction_intent)
        terminal = commands.assert_command(
            commands.execute(account, loser, correction_intent), 200, "rejected"
        )
    assert terminal["error"]["code"] == "revision_conflict"
    assert renewals.relation_model().objects.count() == (
        1 if winner == "renewal" else 0
    )


@pytest.mark.parametrize("operation", ["fence", "purge"])
def test_rm7_fence_or_purge_cannot_cross_an_engaged_renewal(renewal_setup, operation):
    account, old, new = renewal_setup()
    # An unrelated old technical terminal makes purge actually enter its gate.
    spare = account.prescription()
    correction = commands.correction(account, spare)
    terminal = commands.admit(account, correction)
    commands.assert_command(
        commands.execute(account, terminal, correction), 200, "succeeded"
    )
    expired = timezone.now() - timedelta(days=31)
    commands.command_model().objects.filter(pk=terminal["command_id"]).update(
        terminal_at=expired
    )
    intent = renewals.renewal(account, old, new)
    pending = renewals.admit(account, intent)
    reached, resume = threading.Event(), threading.Event()
    table = renewals.relation_model()._meta.db_table

    def request():
        def pause_write(execute_sql, sql, params, many, context):
            if not reached.is_set() and sql.lstrip().upper().startswith(
                f'INSERT INTO "{table.upper()}"'
            ):
                reached.set()
                assert resume.wait(6)
            return execute_sql(sql, params, many, context)

        with connection.execute_wrapper(pause_write):
            return renewals.execute(account, pending, intent)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, request)
        try:
            assert reached.wait(6)
            if operation == "fence":
                reads.assert_error(
                    account.client.post(commands.FENCE, {}, format="json"),
                    503,
                    "service_unavailable",
                )
            else:
                with pytest.raises((DatabaseError, CommandError)):
                    maintenance("purge_expiry_commands")
                assert (
                    commands.command_model()
                    .objects.filter(pk=terminal["command_id"])
                    .exists()
                )
            row = commands.command_model().objects.get(pk=pending["command_id"])
            assert row.state == "pending" and row.terminal_at is None
        finally:
            resume.set()
        succeeded = renewals.assert_command(future.result(timeout=12), 200, "succeeded")
    assert account.client.post(commands.FENCE, {}, format="json").status_code == 200
    assert (
        renewals.assert_command(
            renewals.execute(account, pending, intent), 200, "succeeded"
        )
        == succeeded
    )
    assert renewals.relation_model().objects.count() == 1


@pytest.mark.parametrize("point", ["old_revision", "new_revision", "future"])
def test_rm5_trigger_failure_rolls_back_relation_versions_and_result(
    renewal_setup, point
):
    account, old, new = renewal_setup()
    intent = renewals.renewal(account, old, new)
    pending = renewals.admit(account, intent)
    before = renewals.snapshot(account, old, new)
    table, selected_id = {
        "old_revision": ("nurse_expiryprescriptionstate", old.pk),
        "new_revision": ("nurse_expiryprescriptionstate", new.pk),
        "future": ("nurse_expiryfutureauthority", new.pk),
    }[point]
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE FUNCTION synthetic_282_failure() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'synthetic renewal rollback'; END $$"
        )
        cursor.execute(
            "CREATE TRIGGER synthetic_282_failure BEFORE INSERT OR UPDATE "
            f"ON {table} FOR EACH ROW WHEN (NEW.prescription_id = {selected_id}) "
            "EXECUTE FUNCTION synthetic_282_failure()"
        )
    try:
        account.client.raise_request_exception = False
        hit = []

        def observe_failure(execute_sql, sql, params, many, context):
            try:
                return execute_sql(sql, params, many, context)
            except DatabaseError as error:
                if "synthetic renewal rollback" in str(error):
                    hit.append(point)
                raise

        with connection.execute_wrapper(observe_failure):
            response = renewals.execute(account, pending, intent)
        assert hit == [point], "The requested SQL trigger must cause the failure"
        assert response.status_code in (500, 503)
        assert renewals.snapshot(account, old, new) == before
        assert not renewals.relation_model().objects.exists()
        row = commands.command_model().objects.get(pk=pending["command_id"])
        assert (
            row.state == "pending"
            and row.payload_ciphertext
            and row.terminal_at is None
        )
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TRIGGER synthetic_282_failure ON {table}")
            cursor.execute("DROP FUNCTION synthetic_282_failure()")
    renewals.assert_command(
        renewals.execute(account, pending, intent), 200, "succeeded"
    )


def test_rm5_sql_relation_writer_cannot_cross_an_engaged_confirmation(renewal_setup):
    account, old, new = renewal_setup()
    spare_old, spare_new = account.prescription(), account.prescription()
    renewals.make_source(account, spare_new)
    prior = renewals.confirm(account, spare_old, spare_new)
    model = renewals.relation_model()
    relation = model.objects.get(command_id=prior["command_id"])
    before = model.objects.filter(pk=relation.pk).values().get()
    intent = renewals.renewal(account, old, new)
    pending = renewals.admit(account, intent)
    reached, resume = threading.Event(), threading.Event()
    table = model._meta.db_table

    def put():
        def pause_write(execute_sql, sql, params, many, context):
            if not reached.is_set() and sql.lstrip().upper().startswith(
                f'INSERT INTO "{table.upper()}"'
            ):
                reached.set()
                assert resume.wait(6)
            return execute_sql(sql, params, many, context)

        with connection.execute_wrapper(pause_write):
            return renewals.execute(account, pending, intent)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(on_connection, put)
        try:
            assert reached.wait(6)
            with pytest.raises(DatabaseError) as blocked, transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE {connection.ops.quote_name(table)} "
                        "SET recorded_at=%s WHERE command_id=%s",
                        [timezone.now() + timedelta(seconds=1), relation.command_id],
                    )
            assert blocked.value.__cause__.pgcode == "55P03"
            assert model.objects.filter(pk=relation.pk).values().get() == before
        finally:
            resume.set()
        renewals.assert_command(future.result(timeout=12), 200, "succeeded")
    assert model.objects.count() == 2
