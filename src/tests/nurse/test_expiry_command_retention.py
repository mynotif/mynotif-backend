"""#281 AC4 / RM6–7 / K17–20: bounded technical retention and closed restore."""

import base64
import json
import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection, transaction
from django.utils import timezone
from freezegun import freeze_time

from tests.nurse import test_expiry_commands as commands
from tests.nurse.test_expiry_commands import (
    COMMANDS,
    KEYS,
    NAMESPACE,
    admit,
    assert_command,
    command_model,
    correction,
    execute,
)
from tests.nurse.test_expiry_reads_v4 import assert_error

pytestmark = pytest.mark.django_db(transaction=True)
expiry_settings = commands.expiry_settings
command_settings = commands.command_settings
command_setup = commands.command_setup


def maintenance(name, **options):
    output = StringIO()
    call_command(name, stdout=output, stderr=output, **options)
    return output.getvalue()


def test_k20_pending_body_is_encrypted_and_terminal_payload_is_erased(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    row = command_model().objects.get(pk=pending["command_id"])
    assert row.payload_ciphertext
    serialized = repr(list(command_model().objects.values()))
    assert (
        intent["start_date"] not in serialized and intent["end_date"] not in serialized
    )
    assert h.patient.firstname not in serialized and h.user.username not in serialized
    assert row.payload_hmac and row.key_version == "v1"
    assert row.terminal_at is None
    assert (
        row.user_id == h.user.pk and str(row.origin_session_id) == h.pair["session_id"]
    )
    result = assert_command(execute(h, pending, intent), 200, "succeeded")
    row.refresh_from_db()
    assert not row.payload_ciphertext
    assert row.payload_hmac and row.key_version == "v1" and row.terminal_at is not None
    assert row.result == result["result"]
    serialized = repr(list(command_model().objects.values()))
    assert (
        intent["start_date"] not in serialized and intent["end_date"] not in serialized
    )


def test_k20_rejected_body_is_also_erased(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx, start_date="2027-01-01", end_date="2020-02-29")
    pending = admit(h, intent)
    assert_command(execute(h, pending, intent), 200, "rejected")
    row = command_model().objects.get(pk=pending["command_id"])
    assert not row.payload_ciphertext and row.terminal_at is not None


def test_rm6_retention_starts_at_terminal_not_admission_and_preserves_audit(
    command_setup,
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    admitted_at = timezone.now()
    # Backdate the admission only: a long pending is not an old terminal.
    command_model().objects.filter(pk=pending["command_id"]).update(
        recorded_at=admitted_at - timedelta(days=40)
    )
    assert_command(execute(h, pending, intent), 200, "succeeded")
    terminal_at = command_model().objects.get(pk=pending["command_id"]).terminal_at
    assert terminal_at == admitted_at
    with freeze_time(terminal_at + timedelta(days=29, hours=23, minutes=59)):
        maintenance("purge_expiry_commands")
        assert command_model().objects.filter(pk=pending["command_id"]).exists()
    with freeze_time(terminal_at + timedelta(days=30, seconds=1)):
        maintenance("purge_expiry_commands")
        assert not command_model().objects.filter(pk=pending["command_id"]).exists()
        maintenance("purge_expiry_commands")
    assert (
        command_model("ExpiryCorrectionAudit")
        .objects.filter(command_id=pending["command_id"])
        .exists()
    )
    current = h.detail(rx).json()
    assert current["context"]["mutation_epoch"] != intent["mutation_epoch"]
    assert current["item"]["revision"] != intent["expected_revision"]
    assert_error(execute(h, pending, intent), 404, "not_found")
    # Even the current epoch cannot turn the purged UUID into a new admission.
    assert_error(execute(h, pending, correction(h, rx)), 404, "not_found")


def test_rm6_purge_closes_epoch_and_resolves_its_pending_before_deletion(command_setup):
    h, rx = command_setup()
    first_intent = correction(h, rx)
    first = admit(h, first_intent)
    assert_command(execute(h, first, first_intent), 200, "succeeded")
    second_rx = h.prescription()
    second_intent = correction(h, second_rx)
    pending = admit(h, second_intent)
    before = (second_rx.start_date, second_rx.end_date)
    terminal_at = timezone.now() - timedelta(days=31)
    command_model().objects.filter(pk=first["command_id"]).update(
        recorded_at=terminal_at - timedelta(hours=1), terminal_at=terminal_at
    )
    maintenance("purge_expiry_commands")
    assert not command_model().objects.filter(pk=first["command_id"]).exists()
    abandoned = assert_command(
        h.client.get(f"{COMMANDS}{pending['command_id']}/"), 200, "rejected"
    )
    assert abandoned["error"]["code"] == "command_abandoned"
    commands.assert_unchanged(second_rx, before)
    assert h.page()["context"]["mutation_epoch"] != second_intent["mutation_epoch"]


def test_rm6_age_alone_never_executes_or_rejects_pending_on_get(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    command_model().objects.filter(pk=pending["command_id"]).update(
        recorded_at=timezone.now() - timedelta(days=40)
    )
    row = command_model().objects.get(pk=pending["command_id"])
    assert row.terminal_at is None
    response = h.client.get(f"{COMMANDS}{pending['command_id']}/")
    assert_command(response, 200, "pending")
    row.refresh_from_db()
    assert row.state == "pending" and row.terminal_at is None
    commands.assert_unchanged(rx, (rx.start_date, rx.end_date))


def test_rm6_key_rotation_preserves_exact_replay_until_purge(command_setup, settings):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    terminal = assert_command(execute(h, pending, intent), 200, "succeeded")
    settings.EXPIRY_COMMAND_KEYS = {
        **KEYS,
        "v2": {
            "encryption": base64.b64encode(b"e" * 32).decode(),
            "hmac": base64.b64encode(b"h" * 32).decode(),
        },
    }
    settings.EXPIRY_COMMAND_ACTIVE_KEY = "v2"
    assert assert_command(execute(h, pending, intent), 200, "succeeded") == terminal
    assert_error(
        execute(h, pending, {**intent, "end_date": "2026-04-02"}),
        409,
        "command_conflict",
    )
    fresh = admit(h, correction(h, rx))
    assert command_model().objects.get(pk=fresh["command_id"]).key_version == "v2"
    del settings.EXPIRY_COMMAND_KEYS["v1"]
    assert_error(execute(h, pending, intent), 503, "service_unavailable")


@pytest.mark.parametrize(
    "case",
    [
        "disabled",
        "missing_namespace",
        "mismatched_namespace",
        "missing_key",
        "invalid_key",
        "same_keys",
        "closed",
    ],
)
def test_rm7_runtime_authority_is_fail_closed(command_setup, settings, case):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    changes = {
        "disabled": lambda: setattr(settings, "EXPIRY_COMMANDS_ENABLED", False),
        "missing_namespace": lambda: setattr(settings, "EXPIRY_COMMAND_NAMESPACE", ""),
        "mismatched_namespace": lambda: setattr(
            settings, "EXPIRY_COMMAND_NAMESPACE", str(uuid.uuid4())
        ),
        "missing_key": lambda: setattr(settings, "EXPIRY_COMMAND_KEYS", {}),
        "invalid_key": lambda: setattr(
            settings,
            "EXPIRY_COMMAND_KEYS",
            {"v1": {"encryption": "bad", "hmac": "bad"}},
        ),
        "same_keys": lambda: setattr(
            settings,
            "EXPIRY_COMMAND_KEYS",
            {
                "v1": {
                    "encryption": KEYS["v1"]["encryption"],
                    "hmac": KEYS["v1"]["encryption"],
                }
            },
        ),
        "closed": lambda: command_model("ExpiryCommandAuthority").objects.update(
            accepting=False
        ),
    }
    before = (rx.start_date, rx.end_date)
    changes[case]()
    assert h.detail(rx).json()["can_correct_dates"] is False
    assert_error(execute(h, pending, intent), 503, "service_unavailable")
    assert_error(
        h.client.post(
            COMMANDS, {"kind": "date_correction", "intent": intent}, format="json"
        ),
        503,
        "service_unavailable",
    )
    commands.assert_unchanged(rx, before)


def test_k20_restore_rotation_closes_old_intents_and_never_reopens_automatically(
    command_setup, settings
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    # A new external marker is an operational precondition, not a detector of
    # a physical restore that mistakenly reused the same external namespace.
    new_namespace = str(uuid.uuid4())
    settings.EXPIRY_COMMAND_NAMESPACE = new_namespace
    output = maintenance("close_restored_expiry_commands")
    authority = command_model("ExpiryCommandAuthority").objects.get(pk=1)
    assert str(authority.namespace) == new_namespace != NAMESPACE
    assert authority.accepting is False
    assert h.detail(rx).json()["can_correct_dates"] is False
    row = command_model().objects.get(pk=pending["command_id"])
    assert row.state == "rejected" and row.error["code"] == "command_abandoned"
    assert not row.payload_ciphertext
    assert h.page()["context"]["mutation_epoch"] != intent["mutation_epoch"]
    assert h.patient.firstname not in output and h.user.username not in output
    assert intent["start_date"] not in output and pending["command_id"] not in output
    assert_error(execute(h, pending, intent), 503, "service_unavailable")
    # Explicit synthetic reopening is deliberately separate from the CLI.
    authority.accepting = True
    authority.save(update_fields=["accepting"])
    assert_command(execute(h, pending, intent), 200, "rejected")
    new_intent = correction(h, rx)
    fresh = admit(h, new_intent)
    assert fresh["command_id"] != pending["command_id"]
    assert fresh["mutation_epoch"] != intent["mutation_epoch"]
    assert json.dumps(intent) not in output


@pytest.mark.parametrize(
    "name", ["purge_expiry_commands", "close_restored_expiry_commands"]
)
def test_maintenance_sql_logs_and_sentry_are_private(
    command_setup, settings, caplog, name
):
    import logging

    from nurse.read_privacy import before_breadcrumb, before_send

    h, rx = command_setup()
    pending = admit(h, correction(h, rx))
    if name == "close_restored_expiry_commands":
        settings.EXPIRY_COMMAND_NAMESPACE = str(uuid.uuid4())
    hit = []

    def observe(execute_sql, sql, params, many, context):
        if not hit and "nurse_expirycommand" in sql:
            hit.append(True)
            assert before_send({"message": "SECRET_CLINICAL"}, {}) is None
            assert before_breadcrumb({"message": "SECRET_CLINICAL"}, {}) is None
            logging.getLogger("django.db.backends").error(
                "SECRET_CLINICAL %s",
                pending["command_id"],
                extra={"patient_name": h.patient.firstname, "sql_params": params},
            )
        return execute_sql(sql, params, many, context)

    with connection.execute_wrapper(observe):
        maintenance(name)
    assert hit == [True]
    assert "SECRET_CLINICAL" not in caplog.text
    assert pending["command_id"] not in caplog.text
    assert h.patient.firstname not in caplog.text
    assert not any(
        "patient_name" in record.__dict__ or "sql_params" in record.__dict__
        for record in caplog.records
    )
    event = {"message": "ordinary operational event"}
    assert before_send(event, {}) is event


def test_allocation_namespace_separates_repeated_entropy(command_settings):
    import os
    from unittest.mock import patch

    from nurse.expiry_command_crypto import allocate_command_id

    original_entropy = os.urandom
    principal, epoch = uuid.uuid4(), uuid.uuid4()
    namespaces = (uuid.UUID(NAMESPACE), uuid.uuid4())

    def repeated_allocator_entropy(size):
        return b"r" * 32 if size == 32 else original_entropy(size)

    with patch(
        "nurse.expiry_command_crypto.os.urandom", side_effect=repeated_allocator_entropy
    ):
        identifiers = [
            allocate_command_id(value, principal, epoch, "v1") for value in namespaces
        ]
    assert identifiers[0] != identifiers[1]
    assert all(identifier.version == 4 for identifier in identifiers)


def test_restore_namespace_prevents_reference_reissue_with_repeated_allocator_entropy(
    command_setup, settings
):
    import os
    from unittest.mock import patch

    h, rx = command_setup()
    new_namespace = str(uuid.uuid4())
    original_entropy = os.urandom
    entropy_calls = []

    def repeated_allocator_entropy(size):
        if size == 32:
            entropy_calls.append(size)
            return b"r" * 32
        return original_entropy(size)

    with patch(
        "nurse.expiry_command_crypto.os.urandom", side_effect=repeated_allocator_entropy
    ):
        intent = correction(h, rx)
        old = admit(h, intent)
        assert_command(execute(h, old, intent), 200, "succeeded")
        # Synthetic loss of the technical reference, as in a snapshot predating
        # admission. The clinical audit is kept; no real backup is touched.
        command_model().objects.filter(pk=old["command_id"]).delete()
        settings.EXPIRY_COMMAND_NAMESPACE = new_namespace
        maintenance("close_restored_expiry_commands")
        authority = command_model("ExpiryCommandAuthority").objects.get(pk=1)
        assert authority.accepting is False
        assert str(authority.namespace) == new_namespace
        authority.accepting = True  # Explicit synthetic reopening only.
        authority.save(update_fields=["accepting"])
        fresh_intent = correction(h, rx)
        fresh = admit(h, fresh_intent)
        assert fresh["command_id"] != old["command_id"]
        assert fresh["mutation_epoch"] != old["mutation_epoch"]
        assert_error(h.client.get(f"{COMMANDS}{old['command_id']}/"), 404, "not_found")
        assert_command(execute(h, fresh, fresh_intent), 200, "succeeded")
    assert entropy_calls == [32, 32]
    assert (
        command_model("ExpiryCorrectionAudit")
        .objects.filter(command_id=old["command_id"])
        .exists()
    )


@pytest.fixture
def renewal_sql_setup(command_setup, settings):
    from tests.nurse import test_expiry_renewals as renewals

    settings.EXPIRY_RENEWALS_ENABLED = True
    account, old = command_setup()
    new = account.prescription()
    renewals.make_source(account, new)
    other = renewals.reads.ExpiryAccount()
    other.nurse.patients.add(account.patient)
    renewals.confirm(account, old, new)
    return renewals, account, other, old, new


@pytest.mark.parametrize(
    "writer",
    [
        "update",
        "delete",
        pytest.param(
            "truncate",
            marks=pytest.mark.skipif(
                connection.vendor != "postgresql",
                reason="PostgreSQL statement-level TRUNCATE projection",
            ),
        ),
    ],
)
def test_rm5_raw_renewal_writers_invalidate_both_objects_and_account_collections(
    renewal_sql_setup, writer
):
    """Integrity probes with synthetic SQL do not authorize clinical erasure."""
    renewals, account, other, old, new = renewal_sql_setup
    before = renewals.snapshot(account, old, new)
    other_before = other.page()["context"]["collection_revision"]
    table = connection.ops.quote_name(renewals.relation_model()._meta.db_table)
    statements = {
        "update": f"UPDATE {table} SET recorded_at=recorded_at",
        "delete": f"DELETE FROM {table}",
        "truncate": f"TRUNCATE {table}",
    }
    with connection.cursor() as cursor:
        cursor.execute(statements[writer])
    after = renewals.snapshot(account, old, new)
    assert after["clinical"] == before["clinical"]
    assert (
        after["context"]["collection_revision"]
        != before["context"]["collection_revision"]
    )
    assert other.page()["context"]["collection_revision"] != other_before
    for previous, current in zip(before["items"], after["items"], strict=True):
        assert previous["revision"] != current["revision"]
        future = command_model("ExpiryFutureAuthority").objects.get(
            prescription_id=current["id"]
        )
        assert str(future.revision) == current["revision"]


def test_rm5_missing_renewal_projection_guard_closes_reads_and_admission(
    renewal_sql_setup,
):
    renewals, account, other, old, new = renewal_sql_setup
    intent = renewals.renewal(account, old, new)
    table = connection.ops.quote_name(renewals.relation_model()._meta.db_table)
    with transaction.atomic():
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute(
                    f"ALTER TABLE {table} DISABLE TRIGGER expiry_renewal_update"
                )
            else:
                cursor.execute("DROP TRIGGER expiry_renewal_update")
        assert_error(account.detail(old), 503, "service_unavailable")
        assert_error(renewals.post(account, intent), 503, "service_unavailable")
        transaction.set_rollback(True)
    assert account.detail(old).status_code == 200


def test_rm5_missing_installed_renewal_table_cannot_hide_clinical_history(
    renewal_sql_setup,
):
    renewals, account, other, old, new = renewal_sql_setup
    table = connection.ops.quote_name(renewals.relation_model()._meta.db_table)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE {table}")
        assert_error(account.detail(old), 503, "service_unavailable")
        transaction.set_rollback(True)
    assert account.detail(old).json()["item"]["followup_state"] == "renewed"


def test_rm5_archived_renewal_history_closes_parent_reads_until_restored(
    renewal_sql_setup,
):
    from django.db.migrations.executor import MigrationExecutor

    renewals, account, other, old, new = renewal_sql_setup
    history = list(renewals.relation_model().objects.values())
    try:
        MigrationExecutor(connection).migrate([("nurse", "0023_expiry_command_guards")])
        assert "nurse_expiryrenewal_retained" in connection.introspection.table_names()
        assert_error(account.detail(old), 503, "service_unavailable")
        assert_error(
            account.client.get(renewals.reads.LIST), 503, "service_unavailable"
        )
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
    assert list(renewals.relation_model().objects.values()) == history
    assert account.detail(old).json()["item"]["followup_state"] == "renewed"


RELATION_BARRIER_FAILURES = [
    (event, "drop") for event in ("insert", "update", "delete")
]
if connection.vendor == "postgresql":
    RELATION_BARRIER_FAILURES += [("truncate", "drop")] + [
        (event, "disable") for event in ("insert", "update", "delete", "truncate")
    ]


@pytest.mark.parametrize("event,failure", RELATION_BARRIER_FAILURES)
def test_m1_relation_barrier_failure_closes_both_command_kinds_without_effect(
    command_setup, settings, event, failure
):
    from tests.nurse import test_expiry_renewals as renewals

    settings.EXPIRY_RENEWALS_ENABLED = True
    account, correction_rx = command_setup()
    old, new = account.prescription(), account.prescription()
    renewals.make_source(account, new)
    correction_intent = correction(account, correction_rx, end_date="2026-05-02")
    correction_pending = admit(account, correction_intent)
    renewal_intent = renewals.renewal(account, old, new)
    renewal_pending = renewals.admit(account, renewal_intent)

    def persisted():
        return {
            name: list(command_model(name).objects.order_by("pk").values())
            for name in (
                "Prescription",
                "ExpiryPrescriptionState",
                "ExpiryFutureAuthority",
                "ExpiryAccountState",
                "ExpiryCommand",
                "ExpiryCommandJournal",
                "ExpiryCorrectionAudit",
                "ExpiryRenewal",
            )
        }

    before = persisted()
    table = connection.ops.quote_name("nurse_expiryrenewal")
    trigger = connection.ops.quote_name(f"expiry_cmd_nurse_expiryrenewal_{event}")
    with transaction.atomic():
        with connection.cursor() as cursor:
            if failure == "disable":
                cursor.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
            else:
                suffix = f" ON {table}" if connection.vendor == "postgresql" else ""
                cursor.execute(f"DROP TRIGGER {trigger}{suffix}")
        detail = account.detail(correction_rx)
        assert detail.status_code == 200
        assert detail.json()["can_correct_dates"] is False
        assert detail.json()["can_confirm_renewal"] is False
        assert_error(
            account.client.post(
                COMMANDS,
                {"kind": "date_correction", "intent": correction_intent},
                format="json",
            ),
            503,
            "service_unavailable",
        )
        assert_error(
            execute(account, correction_pending, correction_intent),
            503,
            "service_unavailable",
        )
        assert_error(renewals.post(account, renewal_intent), 503, "service_unavailable")
        assert_error(
            renewals.execute(account, renewal_pending, renewal_intent),
            503,
            "service_unavailable",
        )
        assert persisted() == before
        transaction.set_rollback(True)
    assert persisted() == before
    assert account.detail(correction_rx).json()["can_correct_dates"] is True
    assert_command(
        execute(account, correction_pending, correction_intent), 200, "succeeded"
    )
    renewals.assert_command(
        renewals.execute(account, renewal_pending, renewal_intent), 200, "succeeded"
    )


def test_m1_intact_relation_guards_keep_correction_open_with_renewal_flag_off(
    command_setup, settings
):
    settings.EXPIRY_RENEWALS_ENABLED = False
    account, rx = command_setup()
    detail = account.detail(rx).json()
    assert detail["can_correct_dates"] is True
    assert detail["can_confirm_renewal"] is False
    intent = correction(account, rx, end_date="2026-05-02")
    pending = admit(account, intent)
    assert_command(execute(account, pending, intent), 200, "succeeded")
    rx.refresh_from_db()
    assert rx.end_date.isoformat() == "2026-05-02"


def test_m1_genuine_parent_schema_keeps_correction_runtime_and_reads_available(
    command_setup,
):
    from django.db.migrations.executor import MigrationExecutor

    from nurse.expiry_command_runtime import correction_available

    account, rx = command_setup()
    try:
        MigrationExecutor(connection).migrate([("nurse", "0023_expiry_command_guards")])
        assert "nurse_expiryrenewal" not in connection.introspection.table_names()
        commands.open_runtime()
        assert correction_available() is True
        detail = account.detail(rx)
        assert detail.status_code == 200
        assert detail.json()["can_correct_dates"] is True
        assert detail.json()["can_confirm_renewal"] is False
        assert account.page()["count"] == 1
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
