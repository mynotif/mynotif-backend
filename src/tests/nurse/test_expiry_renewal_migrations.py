"""#282 AC2,4,7 / RM3,5: additive schema, SQL constraints and guard coverage."""

import uuid

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from nurse.models import Prescription
from tests.nurse import test_expiry_renewals as renewals
from tests.nurse.test_expiry_renewals import commands, reads

pytestmark = pytest.mark.django_db(transaction=True)
expiry_settings = renewals.expiry_settings
command_settings = renewals.command_settings
command_setup = renewals.command_setup
renewal_settings = renewals.renewal_settings
renewal_setup = renewals.renewal_setup
OLD = ("nurse", "0023_expiry_command_guards")
SCHEMA = ("nurse", "0024_expiry_renewals")
NEW = ("nurse", "0025_expiry_renewal_guards")


def migrate(target):
    executor = MigrationExecutor(connection)
    assert target in executor.loader.graph.nodes, (
        f"#282 additive migration {target} is required"
    )
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


def test_ac7_declared_renewal_setting_is_closed_by_default():
    from main import settings as declared_settings

    assert getattr(declared_settings, "EXPIRY_RENEWALS_ENABLED", None) is False


def test_ac7_upgrade_reverse_forward_preserves_clinical_sources_and_old_journal(
    expiry_settings,
):
    executor = MigrationExecutor(connection)
    assert NEW in executor.loader.graph.nodes, "#282 renewal migrations are absent"
    migrate(OLD)
    try:
        account = reads.ExpiryAccount()
        account.prescription(start_date="2027-01-01", end_date="2020-02-29")
        account.prescription(start_date=None, end_date=None)
        # Create via historical models: the new nullable command reference must
        # not require current code/model fields while the old schema is active.
        historical = MigrationExecutor(connection).loader.project_state([OLD]).apps
        command = historical.get_model("nurse", "ExpiryCommand")
        state = historical.get_model("nurse", "ExpiryAccountState").objects.get(
            user_id=account.user.pk
        )
        rx = Prescription.objects.filter(patient=account.patient).first()
        identifier = uuid.uuid4()
        command.objects.create(
            command_id=identifier,
            user_id=account.user.pk,
            principal_incarnation=state.incarnation,
            namespace=uuid.uuid4(),
            origin_session_id=account.pair["session_id"],
            mutation_epoch=state.mutation_epoch,
            kind="date_correction",
            key_version="synthetic",
            payload_hmac="0" * 64,
            payload_ciphertext=b"synthetic-pending",
            patient_id=account.patient.pk,
            prescription_id=rx.pk,
        )
        clinical = list(Prescription.objects.order_by("pk").values())
        before = command.objects.filter(pk=identifier).values().get()
        upgraded = migrate(NEW)
        after = (
            upgraded.get_model("nurse", "ExpiryCommand")
            .objects.filter(pk=identifier)
            .values()
            .get()
        )
        assert {key: after[key] for key in before} == before
        assert after["new_prescription_id"] is None
        relation = upgraded.get_model("nurse", "ExpiryRenewal")
        assert not relation.objects.exists()
        assert list(Prescription.objects.order_by("pk").values()) == clinical
        assert (
            upgraded.get_model("nurse", "ExpiryCommandAuthority")
            .objects.get(pk=1)
            .accepting
            is False
        )
        migrate(OLD)
        assert list(Prescription.objects.order_by("pk").values()) == clinical
        migrate(NEW)
        assert list(Prescription.objects.order_by("pk").values()) == clinical
        assert account.detail(rx).json()["can_confirm_renewal"] is False
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_rm5_guard_only_reverse_forward_keeps_relation_history_and_closes_pending(
    renewal_setup,
):
    executor = MigrationExecutor(connection)
    assert NEW in executor.loader.graph.nodes, "#282 guard migration is required"
    account, old, new = renewal_setup()
    renewals.confirm(account, old, new)
    third, fourth = account.prescription(), account.prescription()
    renewals.make_source(account, fourth)
    intent = renewals.renewal(account, third, fourth)
    pending = renewals.admit(account, intent)
    before_history = list(renewals.relation_model().objects.values())
    before_clinical = list(Prescription.objects.order_by("pk").values())
    before_journal = list(commands.command_model().objects.order_by("pk").values())
    try:
        migrate(SCHEMA)
        assert (
            commands.command_model("ExpiryCommandAuthority").objects.get(pk=1).accepting
            is False
        )
        migrate(NEW)
        assert list(renewals.relation_model().objects.values()) == before_history
        assert list(Prescription.objects.order_by("pk").values()) == before_clinical
        assert (
            list(commands.command_model().objects.order_by("pk").values())
            == before_journal
        )
        assert account.detail(third).json()["can_confirm_renewal"] is False
        commands.open_runtime()
        reads.assert_error(
            renewals.execute(account, pending, intent), 409, "command_epoch_closed"
        )
        assert (
            commands.command_model().objects.get(pk=pending["command_id"]).state
            == "pending"
        )
        assert list(renewals.relation_model().objects.values()) == before_history
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_ac7_relation_has_no_clinical_fk_locks_or_technical_retention_cascade(
    renewal_setup,
):
    account, old, new = renewal_setup()
    result = renewals.confirm(account, old, new)
    model = renewals.relation_model()
    clinical_tables = {
        "nurse_prescription",
        "nurse_patient",
        "nurse_nurse",
        "auth_user",
        "main_authsession",
        "nurse_expirycommand",
    }
    for field in model._meta.local_fields:
        if (
            field.is_relation
            and field.remote_field.model._meta.db_table in clinical_tables
        ):
            assert field.db_constraint is False
    before = list(model.objects.values())
    commands.command_model().objects.filter(pk=result["command_id"]).delete()
    assert list(model.objects.values()) == before
    assert (
        account.detail(old).json()["item"]["replacement"]["prescription_id"] == new.pk
    )


@pytest.mark.parametrize("conflict", ["old", "target", "self"])
def test_rm3_database_constraints_protect_unique_link_and_distinct_objects(
    renewal_setup, conflict
):
    account, old, new = renewal_setup()
    renewals.confirm(account, old, new)
    third, fourth = account.prescription(), account.prescription()
    model = renewals.relation_model()
    row = model.objects.values().get()
    row.pop(model._meta.pk.attname)
    row["command_id"] = uuid.uuid4()
    pairs = {
        "old": (old.pk, fourth.pk),
        "target": (third.pk, new.pk),
        "self": (third.pk, third.pk),
    }
    row["old_prescription_id"], row["new_prescription_id"] = pairs[conflict]
    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.create(**row)
    assert model.objects.count() == 1


@pytest.mark.parametrize("event", ["insert", "update", "delete"])
def test_ac7_missing_relation_guard_closes_renewal_capability_and_admission(
    renewal_setup, event
):
    account, old, new = renewal_setup()
    intent = renewals.renewal(account, old, new)
    model = renewals.relation_model()
    table = model._meta.db_table
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            kinds = {"insert": 6, "update": 18, "delete": 10}
            cursor.execute(
                "SELECT t.tgname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname=%s "
                "AND NOT t.tgisinternal AND t.tgtype=%s "
                "AND t.tgenabled IN ('O','A')",
                [table, kinds[event]],
            )
            names = [row[0] for row in cursor.fetchall()]
        else:
            cursor.execute(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name=%s",
                [table],
            )
            names = [
                name
                for name, sql in cursor.fetchall()
                if f"BEFORE {event} ON {table}".upper() in sql.upper()
            ]
    assert names, "Every relation writer must participate in the shared authority"
    with transaction.atomic():
        with connection.cursor() as cursor:
            name = connection.ops.quote_name(names[0])
            if connection.vendor == "postgresql":
                cursor.execute(
                    f"ALTER TABLE {connection.ops.quote_name(table)} "
                    f"DISABLE TRIGGER {name}"
                )
            else:
                cursor.execute(f"DROP TRIGGER {name}")
        assert account.detail(old).json()["can_confirm_renewal"] is False
        reads.assert_error(renewals.post(account, intent), 503, "service_unavailable")
        transaction.set_rollback(True)
    assert account.detail(old).json()["can_confirm_renewal"] is True


def test_rm5_sql_relation_writer_updates_both_versions_and_all_account_collections(
    renewal_setup,
):
    account, old, new = renewal_setup()
    other = reads.ExpiryAccount()
    other.nurse.patients.add(account.patient)
    terminal = renewals.confirm(account, old, new)
    template = (
        renewals.relation_model()
        .objects.values()
        .get(command_id=terminal["command_id"])
    )
    third, fourth = account.prescription(), account.prescription()
    model = renewals.relation_model()
    template.pop(model._meta.pk.attname)
    template.update(
        command_id=uuid.uuid4(),
        old_prescription_id=third.pk,
        new_prescription_id=fourth.pk,
    )
    before = renewals.snapshot(account, third, fourth)
    other_before = other.page()["context"]["collection_revision"]
    # bulk_create skips save/signals. The trigger must maintain authorities even
    # with corrections/renewals closed, just as mixed legacy SQL writers do.
    commands.command_model("ExpiryCommandAuthority").objects.update(accepting=False)
    model.objects.bulk_create([model(**template)])
    after = renewals.snapshot(account, third, fourth)
    assert (
        after["context"]["collection_revision"]
        != before["context"]["collection_revision"]
    )
    assert other.page()["context"]["collection_revision"] != other_before
    for previous, current in zip(before["items"], after["items"], strict=True):
        assert current["revision"] != previous["revision"]
        future = commands.command_model("ExpiryFutureAuthority").objects.get(
            prescription_id=current["id"]
        )
        assert str(future.revision) == current["revision"]
