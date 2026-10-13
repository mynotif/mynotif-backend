import uuid
from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient

from main.session_services import login
from nurse.models import (
    DocumentImport,
    DocumentImportState,
    Nurse,
    Patient,
    Prescription,
    PrescriptionDocumentAsset,
    PrescriptionDocumentVersion,
)
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db


@pytest.fixture
def legacy_and_v3(monkeypatch):
    monkeypatch.setattr(
        "nurse.utils.private_files.signed_file_url",
        lambda field_file: "https://synthetic.invalid/legacy-only",
    )
    user = User.objects.create_user(
        username=f"synthetic-legacy-{uuid.uuid4()}", password=PASSWORD
    )
    pair = login(user.username, PASSWORD)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
    nurse = Nurse.objects.create(user=user)
    patient = Patient.objects.create(firstname="Fictif", lastname="Patient")
    nurse.patients.add(patient)
    legacy = Prescription.objects.create(
        patient=patient,
        prescribing_doctor="Historique",
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        photo_prescription="synthetic-legacy.jpg",
    )
    imported = Prescription.objects.create(
        patient=patient,
        prescribing_doctor="",
        start_date=None,
        end_date=None,
        photo_prescription="private/document-imports/synthetic.pdf",
    )
    operation = DocumentImport.objects.create(
        owner=user,
        patient=patient,
        import_uuid=uuid.uuid4(),
        state=DocumentImportState.CONFIRMED,
        prescription=imported,
        expires_at=timezone.now() + timedelta(hours=24),
    )
    version = PrescriptionDocumentVersion.objects.create(
        prescription=imported,
        document_import=operation,
        version=1,
        prescribing_doctor="",
        start_date=None,
        end_date=None,
    )
    PrescriptionDocumentAsset.objects.create(
        document_import=operation,
        document_version=version,
        role=PrescriptionDocumentAsset.Role.ORIGINAL,
        original_index=0,
        storage_key="private/document-imports/synthetic.pdf",
        mime_type="application/pdf",
        byte_size=11,
        page_count=1,
        sha256="a" * 64,
    )
    return client, patient, legacy, imported


def test_v1_v2_lists_exclude_new_v3_record_before_count(legacy_and_v3):
    client, patient, legacy, imported = legacy_and_v3
    v1 = client.get("/api/v1/prescription/")
    v2 = client.get(f"/api/v2/patients/{patient.pk}/prescriptions/")
    assert v1.status_code == 200
    assert v2.status_code == 200
    assert [row["id"] for row in v1.data] == [legacy.pk]
    assert v2.data["count"] == 1
    assert [row["id"] for row in v2.data["results"]] == [legacy.pk]
    assert imported.pk not in [row["id"] for row in v1.data]


def test_v1_patient_embedded_prescriptions_exclude_v3(legacy_and_v3):
    client, patient, legacy, imported = legacy_and_v3
    response = client.get(f"/api/v1/patient/{patient.pk}/")
    assert response.status_code == 200
    assert [item["id"] for item in response.data["prescriptions"]] == [legacy.pk]
    assert imported.pk not in [item["id"] for item in response.data["prescriptions"]]


@pytest.mark.parametrize(
    "url,method",
    [
        ("/api/v1/prescription/{id}/", "get"),
        ("/api/v1/prescription/{id}/", "patch"),
        ("/api/v1/prescription/{id}/", "delete"),
        ("/api/v1/prescription/{id}/file-url/", "get"),
        ("/api/v1/prescription/upload/{id}/", "put"),
    ],
)
def test_v1_detail_file_and_mutations_hide_v3_before_storage(
    legacy_and_v3, monkeypatch, url, method
):
    client, _, _, imported = legacy_and_v3

    def storage_access(*args, **kwargs):
        raise AssertionError("v3 prescription reached legacy storage")

    monkeypatch.setattr("nurse.utils.private_files.file_exists", storage_access)
    monkeypatch.setattr("nurse.utils.private_files.signed_file_url", storage_access)
    target = url.format(id=imported.pk)
    if method == "patch":
        response = client.patch(target, {"prescribing_doctor": "mutated"})
    elif method == "put":
        response = client.put(target, {"photo_prescription": "replacement"})
    else:
        response = getattr(client, method)(target)
    assert response.status_code == 404
    imported.refresh_from_db()
    assert imported.prescribing_doctor == ""
    assert imported.photo_prescription.name == "private/document-imports/synthetic.pdf"
    assert imported.document_versions.count() == 1
