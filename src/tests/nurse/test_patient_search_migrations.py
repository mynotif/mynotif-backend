import importlib
from unittest.mock import patch

import pytest
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext

from nurse.models import Patient
from nurse.patient_search import SEARCH_COLUMNS, SEARCH_FIELDS, normalize_patient_search

pytestmark = pytest.mark.django_db(transaction=True)

OLD = ("nurse", "0013_alter_prescription_patient")
NEW = ("nurse", "0015_backfill_patient_search")


def test_migrate_old_rows_to_normalized_columns_and_keep_v1_sources():
    executor = MigrationExecutor(connection)
    executor.migrate([OLD])
    try:
        old_apps = executor.loader.project_state([OLD]).apps
        old_patient = old_apps.get_model("nurse", "Patient")
        original = old_patient.objects.create(
            firstname="ÉLODIE", lastname="Straße", city="İstanbul", phone="%_*"
        )
        executor = MigrationExecutor(connection)
        executor.migrate([NEW])
        new_apps = executor.loader.project_state([NEW]).apps
        patient = new_apps.get_model("nurse", "Patient").objects.get(pk=original.pk)
        for field in SEARCH_FIELDS:
            assert getattr(patient, field) == getattr(original, field)
            assert getattr(patient, f"search_{field}") == normalize_patient_search(
                getattr(original, field)
            )
        # Reverse backfill is a no-op: original and derived data remain intact.
        executor = MigrationExecutor(connection)
        executor.migrate([("nurse", "0014_patient_search_columns")])
        assert Patient.objects.get(pk=original.pk).search_firstname == "elodie"
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_backfill_is_idempotent_and_does_not_clobber_changed_source():
    migration = importlib.import_module("nurse.migrations.0015_backfill_patient_search")
    patient = Patient.objects.create(firstname="Before", lastname="Example")
    queryset = models.QuerySet(model=Patient, using=connection.alias)
    queryset.filter(pk=patient.pk).update(search_firstname=None)
    apps = MigrationExecutor(connection).loader.project_state([NEW]).apps
    historical = apps.get_model("nurse", "Patient")
    historical_queryset = type(historical.objects.all())
    real_update = historical_queryset.update
    changed = False

    def race_update(self, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            # Simulate an older writer changing a source after the batch read.
            queryset.filter(pk=patient.pk).update(firstname="After")
        return real_update(self, **kwargs)

    with connection.schema_editor() as editor:
        with patch.object(historical_queryset, "update", race_update):
            migration.backfill_patient_search(apps, editor)
    patient.refresh_from_db()
    assert patient.firstname == "After"
    assert patient.search_firstname is None
    with connection.schema_editor() as editor:
        migration.backfill_patient_search(apps, editor)
        migration.backfill_patient_search(apps, editor)
    patient.refresh_from_db()
    assert patient.search_firstname == "after"
    assert all(getattr(patient, column) is not None for column in SEARCH_COLUMNS)
    assert migration.Migration.atomic is False


def test_postgres_ddl_timeout_applies_before_add_and_reverse_drop():
    migration = importlib.import_module("nurse.migrations.0014_patient_search_columns")
    from unittest.mock import Mock

    editor = Mock()
    editor.connection.vendor = "postgresql"
    editor.connection.alias = "default"
    operation = migration.Migration.operations[0]
    state = MigrationExecutor(connection).loader.project_state([OLD])
    after = state.clone()
    operation.state_forwards("nurse", after)
    operation.database_forwards("nurse", editor, state, after)
    assert editor.method_calls[0].args == ("SET LOCAL lock_timeout = '2s'",)
    editor.reset_mock()
    operation.database_backwards("nurse", editor, after, state)
    assert editor.method_calls[0].args == ("SET LOCAL lock_timeout = '2s'",)


def test_backfill_reads_bounded_keyset_batches():
    migration = importlib.import_module("nurse.migrations.0015_backfill_patient_search")
    apps = MigrationExecutor(connection).loader.project_state([NEW]).apps
    historical = apps.get_model("nurse", "Patient")
    historical.objects.bulk_create(
        [historical(firstname="Élodie", lastname="Synthetic") for _ in range(503)]
    )
    with CaptureQueriesContext(connection) as queries:
        with connection.schema_editor() as editor:
            migration.backfill_patient_search(apps, editor)
    selects = [query["sql"] for query in queries if "SELECT" in query["sql"]]
    assert len(selects) == 3
    assert all("LIMIT 500" in sql and "ORDER BY" in sql for sql in selects)
    assert historical.objects.filter(search_firstname="elodie").count() == 503
