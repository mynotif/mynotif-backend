"""#281 AC7 / RM7: additive schema and reversible synthetic qualification."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from nurse.models import Prescription
from tests.nurse import test_expiry_commands as commands
from tests.nurse.test_expiry_reads_v4 import ExpiryAccount, assert_error

pytestmark = pytest.mark.django_db(transaction=True)
expiry_settings = commands.expiry_settings
OLD = ("nurse", "0021_expiry_read_guards")
NEW = ("nurse", "0023_expiry_command_guards")


def test_rm7_declared_settings_default_is_closed():
    from main import settings as declared_settings

    assert getattr(declared_settings, "EXPIRY_COMMANDS_ENABLED", None) is False


def migrate(target):
    executor = MigrationExecutor(connection)
    assert target in executor.loader.graph.nodes, (
        f"#281 additive migration {target} is required"
    )
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


def test_ac7_upgrade_reverse_forward_preserves_clinical_sources_and_closes_runtime(
    expiry_settings,
):
    executor = MigrationExecutor(connection)
    assert NEW in executor.loader.graph.nodes, (
        "#281 command schema/guard migrations are absent"
    )
    migrate(OLD)
    try:
        account = ExpiryAccount()
        invalid = account.prescription(start_date="2027-01-01", end_date="2020-02-29")
        unknown = account.prescription(start_date=None, end_date=None)
        before = list(Prescription.objects.order_by("pk").values())
        historical = migrate(NEW)
        assert list(Prescription.objects.order_by("pk").values()) == before
        authority_model = historical.get_model("nurse", "ExpiryCommandAuthority")
        authority = authority_model.objects.first()
        assert authority is not None and authority.accepting is False
        assert account.detail(invalid).json()["item"]["date_state"] == "invalid"
        assert account.detail(unknown).json()["item"]["start_date"] is None
        assert account.detail(invalid).json()["can_correct_dates"] is False
        intent = commands.correction(account, invalid)
        assert_error(
            account.client.post(
                commands.COMMANDS,
                {"kind": "date_correction", "intent": intent},
                format="json",
            ),
            503,
            "service_unavailable",
        )
        migrate(OLD)
        assert list(Prescription.objects.order_by("pk").values()) == before
        migrate(NEW)
        assert list(Prescription.objects.order_by("pk").values()) == before
        assert account.detail(invalid).json()["can_correct_dates"] is False
        current = account.page()["context"]["collection_revision"]
        Prescription.objects.filter(pk=unknown.pk).update(end_date="2026-04-01")
        assert account.page()["context"]["collection_revision"] != current
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_ac7_new_tables_are_separate_from_clinical_models_and_default_closed(
    expiry_settings,
):
    names = (
        "ExpiryCommandAuthority",
        "ExpiryCommand",
        "ExpiryCorrectionAudit",
        "ExpiryFutureAuthority",
    )
    models = [commands.command_model(name) for name in names]
    assert all(
        model._meta.db_table in connection.introspection.table_names()
        for model in models
    )
    authority = models[0]()
    assert authority.accepting is False
    clinical_tables = {"nurse_prescription", "nurse_patient", "nurse_nurse"}
    for model in models:
        for field in model._meta.local_fields:
            if (
                field.is_relation
                and field.remote_field.model._meta.db_table in clinical_tables
            ):
                assert field.db_constraint is False
    assert not any(
        field.name.startswith("expiry_command")
        for field in Prescription._meta.local_fields
    )


command_settings = commands.command_settings
command_setup = commands.command_setup


def test_command_guard_only_reverse_forward_is_reapplicable_and_preserves_history(
    command_setup,
):
    account, rx = command_setup()
    intent = commands.correction(account, rx)
    terminal = commands.admit(account, intent)
    commands.assert_command(
        commands.execute(account, terminal, intent), 200, "succeeded"
    )
    second_rx = account.prescription()
    pending_intent = commands.correction(account, second_rx)
    pending = commands.admit(account, pending_intent)
    model = commands.command_model()
    journal_before = list(model.objects.order_by("pk").values())
    audit_model = commands.command_model("ExpiryCorrectionAudit")
    audit_before = list(audit_model.objects.values())
    try:
        migrate(("nurse", "0022_expiry_commands"))
        assert (
            commands.command_model("ExpiryCommandAuthority").objects.get(pk=1).accepting
            is False
        )
        Prescription.objects.filter(pk=rx.pk).update(end_date="2026-05-01")
        clinical_before = list(Prescription.objects.order_by("pk").values())
        migrate(NEW)
        assert list(Prescription.objects.order_by("pk").values()) == clinical_before
        assert list(model.objects.order_by("pk").values()) == journal_before
        assert list(audit_model.objects.values()) == audit_before
        assert (
            commands.command_model("ExpiryCommandAuthority").objects.get(pk=1).accepting
            is False
        )
        assert account.detail(rx).json()["can_correct_dates"] is False
        future = commands.command_model("ExpiryFutureAuthority").objects.get(
            prescription_id=rx.pk
        )
        assert str(future.revision) == account.detail(rx).json()["item"]["revision"]
        commands.open_runtime()
        assert_error(
            commands.execute(account, pending, pending_intent),
            409,
            "command_epoch_closed",
        )
        assert model.objects.get(pk=pending["command_id"]).state == "pending"
        assert list(audit_model.objects.values()) == audit_before
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
