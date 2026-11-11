"""#283 AC3: additive migrations, retained anti-replay and fail-closed guards."""

import uuid

import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from nurse.models import Prescription
from tests.nurse import test_expiry_renewals as renewals
from tests.nurse import test_reminder_reservations as reminders

expiry_settings = reminders.expiry_settings
reminder_setup = reminders.reminder_setup
pytestmark = pytest.mark.django_db(transaction=True)
OLD = ("nurse", "0025_expiry_renewal_guards")
SCHEMA = ("nurse", "0026_expiry_reminders")
NEW = ("nurse", "0027_expiry_reminder_guards")


def migrate(target):
    executor = MigrationExecutor(connection)
    assert target in executor.loader.graph.nodes, (
        f"#283 requires additive migration {target}"
    )
    executor.migrate([target])
    return executor.loader.project_state([target]).apps


def restore_latest():
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def test_ac3_upgrade_preserves_clinical_data_and_starts_closed(expiry_settings):
    assert NEW in MigrationExecutor(connection).loader.graph.nodes, (
        "#283 migrations absent"
    )
    migrate(OLD)
    try:
        account = reminders.reads.ExpiryAccount()
        account.prescription(start_date=None, end_date=None)
        account.prescription(start_date="2027-01-01", end_date="2020-01-01")
        before = list(Prescription.objects.order_by("pk").values())
        upgraded = migrate(NEW)
        authority = upgraded.get_model("nurse", "ExpiryReminderAuthority").objects.get(
            pk=1
        )
        assert authority.accepting is False
        assert list(Prescription.objects.order_by("pk").values()) == before
        assert not upgraded.get_model("nurse", "ExpiryReminderEvent").objects.exists()
    finally:
        restore_latest()


def test_ac3_reverse_forward_retains_admissions_and_closes_restored_runtime(
    reminder_setup,
):
    account, _, _, route, pref = reminder_setup()
    result = reminders.reserve(account, pref, [route])
    models = [
        reminders.model(name)
        for name in (
            "Preference",
            "Opportunity",
            "Event",
            "EventItem",
            "EventRoute",
            "Transition",
        )
    ]
    before = {m._meta.db_table: list(m.objects.order_by("pk").values()) for m in models}
    try:
        migrate(OLD)
        # Physical anti-replay tables must survive removal of Django state.
        for table, rows in before.items():
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) FROM {connection.ops.quote_name(table)}"
                )
                assert cursor.fetchone()[0] == len(rows)
        migrate(NEW)
        for m in models:
            assert list(m.objects.order_by("pk").values()) == before[m._meta.db_table]
        assert reminders.model("Authority").objects.get(pk=1).accepting is False
        reminders.assert_error(
            "service_unavailable", reminders.reserve, account, pref, [route]
        )
        assert reminders.model("Event").objects.filter(pk=result["event_id"]).exists()
    finally:
        restore_latest()


@pytest.mark.parametrize("fault", ["closed", "namespace", "setting"])
def test_ac3_runtime_authority_is_closed_without_explicit_external_configuration(
    reminder_setup, settings, fault
):
    account, _, _, route, pref = reminder_setup()
    actions = {
        "closed": lambda: reminders.model("Authority").objects.update(accepting=False),
        "namespace": lambda: reminders.model("Authority").objects.update(
            namespace=uuid.uuid4()
        ),
        "setting": lambda: setattr(settings, "EXPIRY_REMINDERS_ENABLED", False),
    }
    actions[fault]()
    reminders.assert_error(
        "service_unavailable", reminders.reserve, account, pref, [route]
    )
    assert not reminders.model("Event").objects.exists()


def test_ac3_missing_reservation_guard_fails_closed_before_effect(reminder_setup):
    account, _, _, route, pref = reminder_setup()
    table = reminders.model("Preference")._meta.db_table
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(
                "SELECT t.tgname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname=%s "
                "AND NOT t.tgisinternal AND t.tgenabled IN ('O','A')",
                [table],
            )
            names = [row[0] for row in cursor.fetchall()]
        else:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=%s",
                [table],
            )
            names = [row[0] for row in cursor.fetchall()]
    assert names, "Reservation preference writers require database guards"
    with transaction.atomic():
        with connection.cursor() as cursor:
            guard = connection.ops.quote_name(names[0])
            if connection.vendor == "postgresql":
                cursor.execute(
                    f"ALTER TABLE {connection.ops.quote_name(table)} "
                    f"DISABLE TRIGGER {guard}"
                )
            else:
                cursor.execute(f"DROP TRIGGER {guard}")
        reminders.assert_error(
            "service_unavailable", reminders.reserve, account, pref, [route]
        )
        assert not reminders.model("Event").objects.exists()
        transaction.set_rollback(True)
    reminders.assert_reserved(reminders.reserve(account, pref, [route]))


def test_ac3_sql_preference_writer_cancels_stale_opportunities_atomically(
    reminder_setup,
):
    account, _, _, route, pref = reminder_setup()
    result = reminders.reserve(account, pref, [route])
    preference = reminders.model("Preference")
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {connection.ops.quote_name(preference._meta.db_table)} "
                "SET lead_days=%s,revision=%s WHERE user_id=%s",
                [7, uuid.uuid4().hex, account.user.pk],
            )
        row = reminders.model("Opportunity").objects.get(
            pk=result["opportunity_ids"][0]
        )
        assert row.state == "cancelled" and row.admitted_at is not None
        assert reminders.model("Event").objects.count() == 1
        transaction.set_rollback(True)
    assert reminders.model("Opportunity").objects.get(pk=row.pk).state == "admitted"


def test_ac3_renewal_invalidates_both_prescriptions_without_erasing_daily_quota(
    reminder_setup, settings
):
    account, old, _, route, pref = reminder_setup()
    new = account.prescription(end_date=old.end_date)
    renewals.make_source(account, new)
    result = reminders.reserve(account, pref, [route])
    reminders.assert_reserved(result, 2)
    settings.EXPIRY_COMMANDS_ENABLED = True
    settings.EXPIRY_RENEWALS_ENABLED = True
    settings.EXPIRY_COMMAND_NAMESPACE = renewals.commands.NAMESPACE
    settings.EXPIRY_COMMAND_ACTIVE_KEY = "v1"
    settings.EXPIRY_COMMAND_KEYS = renewals.commands.KEYS
    renewals.commands.open_runtime()
    renewals.confirm(account, old, new)
    rows = reminders.model("Opportunity").objects.filter(
        pk__in=result["opportunity_ids"]
    )
    assert rows.count() == 2 and all(
        r.state == "cancelled" and r.admitted_at is not None for r in rows
    )
    reminders.assert_cancelled_replay(reminders.reserve(account, pref, [route]), result)
    assert reminders.model("Event").objects.count() == 1
