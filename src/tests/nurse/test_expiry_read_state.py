"""Every active writer must invalidate the same durable read generation."""

import pytest
from django.apps import apps
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.db.models import QuerySet
from django.test.utils import CaptureQueriesContext

from nurse.models import Nurse, Patient, Prescription, PrescriptionDocumentAsset
from tests.nurse import test_expiry_reads_v4 as expiry_fixtures
from tests.nurse.test_expiry_reads_v4 import (
    LIST,
    ExpiryAccount,
    assert_error,
    make_document_version,
)

pytestmark = pytest.mark.django_db
expiry_settings = expiry_fixtures.expiry_settings
expiry_setup = expiry_fixtures.expiry_setup
GUARD_TABLES = (
    "auth_user",
    "nurse_prescription",
    "nurse_patient",
    "nurse_nurse",
    "nurse_nurse_patients",
    "nurse_prescriptiondocumentversion",
    "nurse_prescriptiondocumentasset",
    "nurse_documentimport",
)
GUARD_EVENTS = ("insert", "update", "delete") + (
    ("truncate",) if connection.vendor == "postgresql" else ()
)


def guard_name(table, event):
    return "expiry_" + table.removeprefix("nurse_") + "_" + event


def assert_guard_closed(h):
    with CaptureQueriesContext(connection) as queries:
        for _ in range(2):
            assert_error(h.client.get(LIST), 503, "service_unavailable")
    assert not any(
        query["sql"].lstrip().split()[0].upper()
        in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"}
        for query in queries
    )


def snapshot(h):
    h.prescription()
    return h.page(page_size=1)


def assert_invalidated(h, before, *, scope=False):
    after = h.page()
    assert (
        after["context"]["collection_revision"]
        != before["context"]["collection_revision"]
    )
    if scope:
        assert after["context"]["scope_revision"] != before["context"]["scope_revision"]
        assert after["context"]["mutation_epoch"] != before["context"]["mutation_epoch"]
    assert_error(
        h.client.get(LIST, {"page_size": 1, "cursor": before["next_cursor"]}),
        409,
        "pagination_invalidated",
    )
    return after


@pytest.mark.parametrize(
    "writer", ["save", "update", "base_queryset", "bulk_update", "sql"]
)
def test_date_writers_change_object_and_all_associated_account_revisions(
    expiry_setup, writer
):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    shared = ExpiryAccount()
    shared.nurse.patients.add(h.patient)
    unrelated = ExpiryAccount()
    before = snapshot(h)
    shared_before = shared.page()["context"]
    unrelated_before = unrelated.page()["context"]
    revision = h.detail(prescription).json()["item"]["revision"]
    prescription.end_date = None

    def save():
        prescription.save(update_fields=["end_date"])

    def update():
        Prescription.objects.filter(pk=prescription.pk).update(end_date=None)

    def base_queryset():
        QuerySet(model=Prescription).filter(pk=prescription.pk).update(end_date=None)

    def bulk_update():
        Prescription.objects.bulk_update([prescription], ["end_date"])

    def sql():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE nurse_prescription SET end_date = NULL WHERE id = %s",
                [prescription.pk],
            )

    writers = {
        "save": save,
        "update": update,
        "base_queryset": base_queryset,
        "bulk_update": bulk_update,
        "sql": sql,
    }
    writers[writer]()
    assert_invalidated(h, before)
    assert (
        shared.page()["context"]["collection_revision"]
        != shared_before["collection_revision"]
    )
    assert unrelated.page()["context"] == unrelated_before
    item = h.detail(prescription).json()["item"]
    assert item["revision"] != revision
    assert item["end_date"] is None and item["date_state"] == "unknown"


@pytest.mark.parametrize(
    "writer", ["create", "bulk_create", "delete", "queryset_delete"]
)
def test_membership_writers_invalidate_count_and_cursor(expiry_setup, writer):
    h = expiry_setup
    before = snapshot(h)
    prescription = Prescription.objects.filter(patient=h.patient).first()
    actions = {
        "create": lambda: h.prescription(),
        "bulk_create": lambda: Prescription.objects.bulk_create(
            [
                Prescription(
                    patient=h.patient,
                    prescribing_doctor="Import synthétique",
                    photo_prescription="",
                ),
            ]
        ),
        "delete": prescription.delete,
        "queryset_delete": lambda: Prescription.objects.filter(
            pk=prescription.pk
        ).delete(),
    }
    actions[writer]()
    after = assert_invalidated(h, before)
    expected_delta = {
        "create": 1,
        "bulk_create": 1,
        "delete": -1,
        "queryset_delete": -1,
    }
    assert after["count"] == before["count"] + expected_delta[writer]


@pytest.mark.parametrize(
    "field,value",
    [
        ("prescribing_doctor", "Autre prescripteur"),
        ("photo_prescription", "new-synthetic.png"),
    ],
)
def test_prescription_metadata_writers_invalidate_object_revision(
    expiry_setup, field, value
):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    before = snapshot(h)
    revision = h.detail(prescription).json()["item"]["revision"]
    Prescription.objects.filter(pk=prescription.pk).update(**{field: value})
    assert_invalidated(h, before)
    assert h.detail(prescription).json()["item"]["revision"] != revision


@pytest.mark.parametrize("field", ["firstname", "lastname"])
def test_patient_names_invalidate_the_projection_even_for_base_queryset(
    expiry_setup, field
):
    h = expiry_setup
    before = snapshot(h)
    QuerySet(model=Patient).filter(pk=h.patient.pk).update(**{field: "Nom modifié"})
    after = assert_invalidated(h, before)
    assert all(item["patient"][field] == "Nom modifié" for item in after["results"])


@pytest.mark.parametrize("inverse", [False, True])
@pytest.mark.parametrize(
    "writer", ["remove", "clear", "through_delete", "through_reassign"]
)
def test_direct_inverse_and_through_association_writers_close_old_scope(
    expiry_setup, inverse, writer
):
    h = expiry_setup
    before = snapshot(h)
    relation = h.patient.nurse_set if inverse else h.nurse.patients
    target = h.nurse if inverse else h.patient
    through = Nurse.patients.through
    link = through.objects.get(nurse_id=h.nurse.pk, patient_id=h.patient.pk)
    other_patient = Patient.objects.create(firstname="Autre", lastname="Synthétique")
    actions = {
        "remove": lambda: relation.remove(target),
        "clear": relation.clear,
        "through_delete": link.delete,
        "through_reassign": lambda: through.objects.filter(pk=link.pk).update(
            patient_id=other_patient.pk
        ),
    }
    actions[writer]()
    after = assert_invalidated(h, before, scope=True)
    assert after["count"] == 0
    # Regranting access must not resurrect a reference issued before removal.
    relation.add(target)
    assert_invalidated(h, before, scope=True)


@pytest.mark.parametrize("inverse", [False, True])
def test_new_association_invalidates_scope_in_both_directions(expiry_setup, inverse):
    h = expiry_setup
    before = snapshot(h)
    patient = Patient.objects.create(firstname="Nouveau", lastname="Patient")
    Prescription.objects.create(
        patient=patient, prescribing_doctor="Dr", photo_prescription=""
    )
    if inverse:
        patient.nurse_set.add(h.nurse)
    else:
        h.nurse.patients.add(patient)
    after = assert_invalidated(h, before, scope=True)
    assert after["count"] == before["count"] + 1


def test_reassignment_moves_authorized_object_and_invalidates_both_accounts(
    expiry_setup,
):
    h = expiry_setup
    other = ExpiryAccount()
    prescription = Prescription.objects.get(patient=h.patient)
    before = snapshot(h)
    other_before = other.page()["context"]
    Prescription.objects.filter(pk=prescription.pk).update(patient=other.patient)
    assert_invalidated(h, before)
    assert_error(h.detail(prescription), 404, "not_found")
    assert other.detail(prescription).status_code == 200
    assert (
        other.page()["context"]["collection_revision"]
        != other_before["collection_revision"]
    )


def test_nurse_user_reassignment_does_not_reuse_the_old_scope(expiry_setup):
    h = expiry_setup
    replacement = ExpiryAccount()
    replacement.nurse.delete()
    before = snapshot(h)
    Nurse.objects.filter(pk=h.nurse.pk).update(user=replacement.user)
    assert_error(h.client.get(LIST), 404, "not_found")
    assert replacement.page()["count"] == before["count"]


def test_user_update_rotates_scope_and_invalidates_previous_cursor(expiry_setup):
    h = expiry_setup
    before = snapshot(h)
    User.objects.filter(pk=h.user.pk).update(first_name="Compte modifié")
    assert_invalidated(h, before, scope=True)


def test_user_delete_revokes_reads_and_removes_account_authority(expiry_setup):
    from nurse.expiry_models import ExpiryAccountState

    h = expiry_setup
    user_id = h.user.pk
    assert h.page()["count"] == 1
    h.user.delete()
    assert_error(h.client.get(LIST), 401, "authentication_required")
    assert not ExpiryAccountState.objects.filter(user_id=user_id).exists()


def test_nurse_delete_and_recreate_cannot_revive_old_scope(expiry_setup):
    h = expiry_setup
    before = snapshot(h)
    h.nurse.delete()
    assert_error(h.client.get(LIST), 404, "not_found")
    h.nurse = Nurse.objects.create(user=h.user)
    h.nurse.patients.add(h.patient)
    after = assert_invalidated(h, before, scope=True)
    assert after["count"] == before["count"]


@pytest.mark.parametrize("writer", ["through", "document_metadata"])
def test_direct_sql_writers_invalidate_current_projection(expiry_setup, writer):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    imported, _, _ = make_document_version(h, prescription)
    before = snapshot(h)
    revision = h.detail(prescription).json()["item"]["revision"]
    statements = {
        "through": (
            "DELETE FROM nurse_nurse_patients WHERE nurse_id=%s AND patient_id=%s",
            [h.nurse.pk, h.patient.pk],
        ),
        "document_metadata": (
            "UPDATE nurse_documentimport SET metadata=NULL WHERE id=%s",
            [type(imported)._meta.pk.get_db_prep_value(imported.pk, connection)],
        ),
    }
    with connection.cursor() as cursor:
        cursor.execute(*statements[writer])
    after = assert_invalidated(h, before, scope=writer == "through")
    if writer == "through":
        assert after["count"] == 0
        assert_error(h.detail(prescription), 404, "not_found")
    else:
        assert h.detail(prescription).json()["item"]["revision"] != revision


@pytest.mark.parametrize(
    "writer",
    [
        "version_create",
        "version_update",
        "asset_update",
        "asset_delete",
        "import_update",
    ],
)
def test_document_writers_invalidate_revision_and_collection(expiry_setup, writer):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    imported, version, asset = make_document_version(h, prescription)
    before = snapshot(h)
    revision = h.detail(prescription).json()["item"]["revision"]
    actions = {
        "version_create": lambda: make_document_version(
            h, prescription, version_number=2
        ),
        "version_update": lambda: type(version)
        .objects.filter(pk=version.pk)
        .update(version=2),
        "asset_update": lambda: PrescriptionDocumentAsset.objects.filter(
            pk=asset.pk
        ).update(storage_key=f"changed/{asset.pk}"),
        "asset_delete": asset.delete,
        "import_update": lambda: type(imported)
        .objects.filter(pk=imported.pk)
        .update(metadata={"pages": [{"asset_index": 99}]}),
    }
    actions[writer]()
    assert_invalidated(h, before)
    assert h.detail(prescription).json()["item"]["revision"] != revision


def test_rollback_preserves_generation_revision_and_old_cursor(expiry_setup):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    before = snapshot(h)
    revision = h.detail(prescription).json()["item"]["revision"]
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with transaction.atomic():
            Prescription.objects.filter(pk=prescription.pk).update(end_date=None)
            h.nurse.patients.remove(h.patient)
            raise RuntimeError("synthetic rollback")
    assert h.page()["context"] == before["context"]
    assert h.detail(prescription).json()["item"]["revision"] == revision
    assert (
        h.client.get(
            LIST, {"page_size": 1, "cursor": before["next_cursor"]}
        ).status_code
        == 200
    )


def test_absent_guard_fails_closed_without_repair_by_get(expiry_setup):
    h = expiry_setup
    with transaction.atomic():
        with connection.cursor() as cursor:
            if connection.vendor == "sqlite":
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='nurse_prescription'"
                )
                triggers = [row[0] for row in cursor.fetchall()]
                assert triggers, "Prescription writers must have database guards"
                for name in triggers:
                    cursor.execute(f"DROP TRIGGER {connection.ops.quote_name(name)}")
            else:
                cursor.execute(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'nurse_prescription'::regclass "
                    "AND NOT tgisinternal"
                )
                triggers = [row[0] for row in cursor.fetchall()]
                assert triggers, "Prescription writers must have database guards"
                for name in triggers:
                    cursor.execute(
                        f"DROP TRIGGER {connection.ops.quote_name(name)} "
                        "ON nurse_prescription"
                    )
        assert_error(h.client.get(LIST), 503, "service_unavailable")
        transaction.set_rollback(True)
    assert h.client.get(LIST).status_code == 200


@pytest.mark.parametrize("table", GUARD_TABLES)
@pytest.mark.parametrize("event", GUARD_EVENTS)
def test_each_missing_writer_guard_closes_reads_without_get_repair(
    expiry_setup, table, event
):
    h = expiry_setup
    assert h.client.get(LIST).status_code == 200
    with transaction.atomic():
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            statement = (
                f"DROP TRIGGER {connection.ops.quote_name(guard_name(table, event))}"
            )
            if connection.vendor == "postgresql":
                statement += f" ON {connection.ops.quote_name(table)}"
            cursor.execute(statement)
        assert_guard_closed(h)
        transaction.set_rollback(True)
    assert h.client.get(LIST).status_code == 200


def test_absent_prescription_authority_fails_closed_without_get_backfill(expiry_setup):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    # Discover technical sidecars by relationship rather than model name.
    states = [
        (model, field)
        for model in apps.get_app_config("nurse").get_models()
        if model.__module__ == "nurse.expiry_models"
        for field in model._meta.fields
        if getattr(field, "related_model", None) is Prescription
    ]
    assert states, "An existing prescription must have durable read authority"
    with transaction.atomic():
        with connection.cursor() as cursor:
            for model, field in states:
                cursor.execute(
                    f"DELETE FROM {connection.ops.quote_name(model._meta.db_table)} "
                    f"WHERE {connection.ops.quote_name(field.column)} = %s",
                    [prescription.pk],
                )
        assert_error(h.client.get(LIST), 503, "service_unavailable")
        transaction.set_rollback(True)
    assert h.client.get(LIST).status_code == 200
