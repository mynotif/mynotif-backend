from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection, models
from django.test.utils import CaptureQueriesContext

from nurse.models import Nurse, Patient
from nurse.patient_search import SEARCH_COLUMNS, SEARCH_FIELDS, normalize_patient_search
from nurse.serializers import PatientSerializer

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "source,expected",
    [
        ("ÉLODIE", "elodie"),
        ("e\u0301lodie", "elodie"),
        ("Straße", "strasse"),
        ("ΟΣ ς σ", "οσ σ σ"),
        ("İstanbul", "istanbul"),
        ("Æ Œ", "æ œ"),
        ("a\u20ddb\u0903c", "abc"),
        ("%_*", "%_*"),
    ],
)
def test_contract_normalization(source, expected):
    assert normalize_patient_search(source) == expected


def assert_derived(patient):
    patient.refresh_from_db()
    for field in SEARCH_FIELDS:
        assert getattr(patient, f"search_{field}") == normalize_patient_search(
            getattr(patient, field)
        )


def test_create_save_and_partial_save():
    patient = Patient.objects.create(
        firstname="Élodie", lastname="Straße", city="LYON", phone="06"
    )
    assert_derived(patient)
    patient.firstname = "İpek"
    patient.city = "Do not persist"
    patient.save(update_fields=["firstname"])
    assert_derived(patient)
    assert patient.city == "LYON"
    patient.lastname = "Σς"
    patient.save()
    assert_derived(patient)


def test_deferred_save_updates_only_loaded_source():
    patient = Patient.objects.create(firstname="First", lastname="Last", city="Évry")
    deferred = Patient.objects.only("firstname").get(pk=patient.pk)
    deferred.firstname = "Élodie"
    with CaptureQueriesContext(connection) as queries:
        deferred.save()
    assert len(queries) == 1
    assert_derived(patient)
    assert patient.city == "Évry"


def test_update_is_atomic_and_literal():
    patient = Patient.objects.create(firstname="Initial", lastname="Last")
    Patient.objects.filter(pk=patient.pk).update(firstname="ÉLODIE", city="LYON")
    assert_derived(patient)
    for value in (models.F("lastname"), models.Value("Other"), None, 42):
        with pytest.raises(ValueError, match="literal strings"):
            Patient.objects.filter(pk=patient.pk).update(firstname=value)
    assert_derived(patient)


def test_derived_columns_cannot_be_written_explicitly():
    patient = Patient.objects.create(firstname="First", lastname="Last")
    with pytest.raises(ValueError, match="derived"):
        Patient.objects.update(search_firstname="forged")
    with pytest.raises(ValueError, match="derived"):
        patient.save(update_fields=["search_firstname"])
    with pytest.raises(ValueError, match="derived"):
        Patient.objects.bulk_update([patient], ["search_firstname"])
    with pytest.raises(ValueError, match="derived"):
        Patient.objects.bulk_create(
            [patient], update_conflicts=True, update_fields=["search_firstname"]
        )


def test_bulk_create_update_and_conflict_update():
    patients = [
        Patient(firstname="Élodie", lastname="A"),
        Patient(firstname="İpek", lastname="B"),
    ]
    Patient.objects.bulk_create(iter(patients), batch_size=1)
    for patient in patients:
        assert_derived(patient)
        patient.city = "ÉVRY"
    assert Patient.objects.bulk_update(iter(patients), ["city"], batch_size=1) == 2
    for patient in patients:
        assert_derived(patient)
    replacement = Patient(pk=patients[0].pk, firstname="Straße", lastname="Ignored")
    Patient.objects.bulk_create(
        [replacement],
        update_conflicts=True,
        update_fields=["firstname"],
        unique_fields=["pk"],
    )
    assert_derived(patients[0])
    assert patients[0].firstname == "Straße"
    assert patients[0].lastname == "A"


def test_bulk_update_respects_queryset_scope_and_rejects_expressions():
    first = Patient.objects.create(firstname="A", lastname="A")
    second = Patient.objects.create(firstname="B", lastname="B")
    first.city = second.city = "Évry"
    assert (
        Patient.objects.filter(pk=first.pk).bulk_update([first, second], ["city"]) == 1
    )
    assert_derived(first)
    assert_derived(second)
    assert second.city == ""
    first.firstname = models.F("lastname")
    with pytest.raises(ValueError, match="literal strings"):
        Patient.objects.bulk_update([first], ["firstname"])
    with pytest.raises(ValueError, match="literal strings"):
        Patient.objects.bulk_create([Patient(firstname=models.Value("A"))])


def test_search_global_sql_and_terms_or_fields_and_scope(user):
    nurse = Nurse.objects.create(user=user)
    patients = Patient.objects.bulk_create(
        [Patient(firstname=f"Synthetic {i}", lastname="Example") for i in range(25)]
    )
    target = Patient.objects.create(firstname="ÉLODIE", lastname="Straße", city="Lyon")
    foreign = Patient.objects.create(firstname="Élodie", lastname="Straße", city="Lyon")
    nurse.patients.add(*patients, target)
    scoped = Patient.objects.filter(nurse__user=user)
    with CaptureQueriesContext(connection) as queries:
        found = scoped.search_normalized("ELODIE\u2003lyon STRASSE").order_by("id")
        assert found.count() == 1
        assert list(found.values_list("pk", flat=True)[:20]) == [target.pk]
    assert len(queries) == 2
    assert "COUNT(" in queries[0]["sql"]
    assert "LIMIT 20" in queries[1]["sql"]
    assert foreign.pk not in found.values_list("pk", flat=True)
    assert list(scoped.order_by("id").values_list("pk", flat=True)[:20]) == [
        patient.pk for patient in patients[:20]
    ]
    assert scoped.search_normalized("Elodie Marseille").count() == 0
    assert scoped.search_normalized(" \u2003\t").count() == 26


@pytest.mark.parametrize("term", ["%", "_", "*", "\\"])
def test_wildcards_are_literal(term):
    target = Patient.objects.create(firstname=f"A{term}B", lastname="Example")
    Patient.objects.create(firstname="Ordinary", lastname="Example")
    assert list(Patient.objects.search_normalized(term)) == [target]


def test_v1_serializer_has_exact_original_fields():
    patient = Patient.objects.create(firstname="Élodie", lastname="Example")
    assert set(PatientSerializer(patient).data) == {
        "id",
        "firstname",
        "lastname",
        "street",
        "zip_code",
        "city",
        "phone",
        "birthday",
        "health_card_number",
        "ss_provider_code",
        "prescriptions",
        "expire_soon_prescriptions",
    }
    assert set(PatientSerializer(patient, fields=["id", "firstname"]).data) == {
        "id",
        "firstname",
    }
    assert not set(SEARCH_COLUMNS).intersection(PatientSerializer(patient).data)


def test_repair_rebuilds_stale_and_null_columns_without_logging(caplog):
    patient = Patient.objects.create(firstname="SYNTHETIC_PRIVATE_MARKER", lastname="A")
    raw = models.QuerySet(model=Patient, using="default")
    raw.filter(pk=patient.pk).update(search_firstname=None, search_city="stale")
    output = StringIO()
    call_command("repair_patient_search", batch_size=1, stdout=output)
    assert_derived(patient)
    assert "SYNTHETIC_PRIVATE_MARKER" not in output.getvalue() + caplog.text
    assert str(patient.pk) not in output.getvalue()
    call_command("repair_patient_search", check=True, batch_size=1, stdout=output)


def test_save_empty_update_fields_is_noop_and_full_save_recomputes():
    patient = Patient.objects.create(firstname="Initial", lastname="Example")
    patient.firstname = "Élodie"
    patient.search_firstname = "forged"
    with CaptureQueriesContext(connection) as queries:
        patient.save(update_fields=[])
    assert len(queries) == 0
    patient.save()
    assert_derived(patient)
    assert patient.search_firstname == "elodie"


def test_update_unrelated_expression_remains_supported():
    patient = Patient.objects.create(firstname="Élodie", lastname="Example")
    Patient.objects.filter(pk=patient.pk).update(ss_provider_code=models.Value("123"))
    assert_derived(patient)
    assert patient.ss_provider_code == "123"


def test_casefold_expansion_not_truncated():
    patient = Patient.objects.create(firstname="ß" * 30, lastname="Example")
    assert_derived(patient)
    assert patient.search_firstname == "ss" * 30
    assert list(Patient.objects.search_normalized("ss" * 30)) == [patient]


def test_repair_check_failure_and_invalid_batch_are_generic():
    from django.core.management.base import CommandError

    patient = Patient.objects.create(firstname="PrivateSynthetic", lastname="Example")
    models.QuerySet(model=Patient).filter(pk=patient.pk).update(search_firstname=None)
    with pytest.raises(CommandError, match="inconsistent"):
        call_command("repair_patient_search", check=True, stdout=StringIO())
    for size in (0, 1001):
        with pytest.raises(CommandError, match="Batch size"):
            call_command("repair_patient_search", batch_size=size, stdout=StringIO())


def test_nul_search_cannot_match_postgresql_text():
    Patient.objects.create(firstname="Synthetic", lastname="Example")
    assert Patient.objects.search_normalized("\x00").count() == 0


@pytest.mark.skipif(connection.vendor != "sqlite", reason="PostgreSQL text forbids NUL")
def test_sqlite_search_is_literal_across_embedded_nul():
    target = Patient.objects.create(firstname="A\x00Needle", lastname="Example")
    Patient.objects.create(firstname="Ordinary", lastname="Example")
    assert list(Patient.objects.search_normalized("needle")) == [target]
    assert list(Patient.objects.search_normalized("\x00")) == [target]
