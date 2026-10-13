import json
import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.db import connection, connections
from PIL import Image
from rest_framework.test import APIClient

from nurse import document_import_services
from nurse.document_scanner import ScannerUnavailable
from django.utils import timezone

from nurse.models import (
    DocumentImport,
    DocumentImportAttempt,
    DocumentImportState,
    Nurse,
    Patient,
    Prescription,
    PrescriptionDocumentAsset,
    PrescriptionDocumentVersion,
)

pytestmark = pytest.mark.django_db


def synthetic_png():
    stream = BytesIO()
    Image.new("RGB", (2, 2), color=(40, 50, 60)).save(stream, format="PNG")
    return stream.getvalue()


def synthetic_jpeg():
    stream = BytesIO()
    Image.new("RGB", (2, 2), color=(70, 80, 90)).save(stream, format="JPEG")
    return stream.getvalue()


def api_context():
    user = User.objects.create_user(username=f"synthetic-import-{uuid.uuid4()}")
    nurse = Nurse.objects.create(user=user)
    patient = Patient.objects.create(firstname="Fictif", lastname="Import")
    nurse.patients.add(patient)
    client = APIClient()
    client.force_authenticate(user)
    return client, patient, user


def request_payload(patient_id, import_id, *, doctor="", content=None):
    content = content or synthetic_png()
    metadata = {
        "prescribing_doctor": doctor,
        "start_date": None,
        "end_date": None,
        "source_kind": "image_import",
        "pages": [
            {"asset_index": 0, "source_page": 1, "rotation": 0, "crop": None}
        ],
    }
    return (
        f"/api/v3/patients/{patient_id}/document-imports/",
        {
            "metadata": json.dumps(metadata),
            "original_0": SimpleUploadedFile(
                "synthetic.png", content, content_type="image/png"
            ),
        },
        str(import_id),
    )


@pytest.fixture
def ready_backend(monkeypatch):
    monkeypatch.setattr(document_import_services, "scan_bytes", lambda *args: True)
    monkeypatch.setattr(
        document_import_services,
        "validate_document",
        lambda content: {
            "mime_type": "image/png",
            "page_count": 1,
            "width": 2,
            "height": 2,
            "image_pixels": 4,
            "sha256": __import__("hashlib").sha256(content).hexdigest(),
            "byte_size": len(content),
        },
    )
    derivative = synthetic_jpeg()
    monkeypatch.setattr(
        "nurse.document_import_views.derive_image",
        lambda _content, _recipe: (
            derivative,
            {
                "mime_type": "image/jpeg",
                "byte_size": len(derivative),
                "sha256": hashlib.sha256(derivative).hexdigest(),
            },
        ),
    )
    return derivative


def fs_storage(tmp_path):
    return override_settings(
        STORAGES={
            "default": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": str(tmp_path)},
            },
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            },
        }
    )


def test_changed_initial_content_conflicts_for_same_account_uuid(
    tmp_path, ready_backend
):
    client, patient, _ = api_context()
    import_id = uuid.uuid4()
    url, data, key = request_payload(patient.pk, import_id)
    with fs_storage(tmp_path):
        first = client.post(
            url, data=data, format="multipart", HTTP_IDEMPOTENCY_KEY=key
        )
        conflict_url, conflict_data, conflict_key = request_payload(
            patient.pk, import_id, content=synthetic_png() + b"changed"
        )
        conflict = client.post(
            conflict_url,
            data=conflict_data,
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=conflict_key,
        )
    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.data["code"] == "idempotency_conflict"
    assert DocumentImport.objects.filter(import_uuid=import_id).count() == 1
    assert Prescription.objects.count() == 0


def test_patch_revision_and_all_human_confirmations_gate_single_record(
    tmp_path, ready_backend
):
    client, patient, _ = api_context()
    derivative = ready_backend
    import_id = uuid.uuid4()
    url, data, key = request_payload(patient.pk, import_id)
    original_bytes = data["original_0"].read()
    data["original_0"].seek(0)
    with fs_storage(tmp_path):
        created = client.post(
            url, data=data, format="multipart", HTTP_IDEMPOTENCY_KEY=key
        )
        draft_url = f"{url}{import_id}/"
        changed_metadata = {
            "prescribing_doctor": "Saisi manuellement",
            "start_date": None,
            "end_date": None,
            "source_kind": "image_import",
            "pages": [
                {"asset_index": 0, "source_page": 1, "rotation": 90, "crop": None}
            ],
        }
        changed = client.patch(
            draft_url,
            changed_metadata,
            format="json",
            HTTP_IF_MATCH='"1"',
        )
        confirmation = f"{draft_url}confirmation/"
        stale = client.put(
            confirmation,
            {
                "revision": 1,
                "patient_confirmed": True,
                "pages_confirmed": True,
                "dates_reviewed": True,
            },
            format="json",
        )
        incomplete = client.put(
            confirmation,
            {
                "revision": 2,
                "patient_confirmed": True,
                "pages_confirmed": True,
                "dates_reviewed": False,
            },
            format="json",
        )
        recorded = client.put(
            confirmation,
            {
                "revision": 2,
                "patient_confirmed": True,
                "pages_confirmed": True,
                "dates_reviewed": True,
            },
            format="json",
        )
    assert created.status_code == 202
    assert changed.status_code == 200, changed.data
    assert changed.data["revision"] == 2
    assert stale.status_code == 412
    assert incomplete.status_code == 409
    assert Prescription.objects.filter(patient=patient).count() == 1
    assert recorded.status_code == 201, recorded.data
    assert recorded.data["revision"] == 2
    assert recorded.data["start_date"] is None
    assert recorded.data["end_date"] is None
    assert recorded.data["prescribing_doctor"] == "Saisi manuellement"
    assert PrescriptionDocumentVersion.objects.count() == 1
    assert PrescriptionDocumentAsset.objects.filter(
        document_version__isnull=False
    ).count() == 2
    source = next(asset for asset in recorded.data["assets"] if asset["role"] == "original")
    display = next(asset for asset in recorded.data["assets"] if asset["role"] == "derived")
    assert recorded.data["pages"][0]["source_asset_id"] == source["asset_id"]
    assert recorded.data["pages"][0]["display_asset_id"] == display["asset_id"]
    with fs_storage(tmp_path):
        original = client.get(
            f"{draft_url}assets/{source['asset_id']}/content/"
        )
        preview = client.get(
            f"{draft_url}assets/{display['asset_id']}/content/"
        )
    original_content = b"".join(original.streaming_content)
    preview_content = b"".join(preview.streaming_content)
    assert hashlib.sha256(original_content).hexdigest() == source["sha256"]
    assert original_content == original_bytes
    assert preview_content == derivative


def test_access_to_foreign_draft_uses_same_not_found():
    client, patient, _ = api_context()
    unknown_uuid = uuid.uuid4()
    response = client.get(
        f"/api/v3/patients/{patient.pk}/document-imports/{unknown_uuid}/"
    )
    assert response.status_code == 404
    assert response.data == {
        "code": "not_found",
        "message": "Ressource indisponible.",
        "fields": {},
    }


def test_import_list_uses_m03_closed_pagination(ready_backend):
    client, patient, user = api_context()
    for _ in range(3):
        DocumentImport.objects.create(
            owner=user,
            patient=patient,
            import_uuid=uuid.uuid4(),
            state=DocumentImportState.RECEIVING,
            expires_at=timezone.now() + timedelta(hours=24),
        )
    first = client.get(
        f"/api/v3/patients/{patient.pk}/document-imports/?page=1&page_size=2"
    )
    second = client.get(
        f"/api/v3/patients/{patient.pk}/document-imports/?page=2&page_size=2"
    )
    invalid = client.get(
        f"/api/v3/patients/{patient.pk}/document-imports/?page=01"
    )
    assert first.status_code == second.status_code == 200
    assert first.data["count"] == 3
    assert first.data["page"] == 1
    assert first.data["page_size"] == 2
    assert first.data["next_page"] == 2
    assert first.data["previous_page"] is None
    assert len(first.data["results"]) == 2
    assert second.data["previous_page"] == 1
    assert len(second.data["results"]) == 1
    assert invalid.status_code == 400


def test_upload_rate_limit_counts_retries_before_parsing_multipart(tmp_path, ready_backend, monkeypatch):
    client, patient, user = api_context()
    responses = []
    import_id = uuid.uuid4()
    url, _, key = request_payload(patient.pk, import_id)
    scans = []

    def unavailable_scan(*args):
        scans.append(args)
        raise ScannerUnavailable()

    monkeypatch.setattr(document_import_services, "scan_bytes", unavailable_scan)
    with fs_storage(tmp_path):
        for _ in range(3):
            _, data, _ = request_payload(patient.pk, import_id)
            responses.append(
                client.post(url, data=data, format="multipart", HTTP_IDEMPOTENCY_KEY=key)
            )
        responses.append(
            client.generic(
                "POST", url, data=b"{", content_type="application/json",
                HTTP_IDEMPOTENCY_KEY=key,
            )
        )
    assert [response.status_code for response in responses] == [503, 503, 503, 429]
    assert responses[-1].data["code"] == "rate_limited"
    assert len(scans) == 3
    assert DocumentImport.objects.filter(owner=user).count() == 1
    assert DocumentImport.objects.get(owner=user).state == DocumentImportState.RETRYABLE
    assert DocumentImportAttempt.objects.filter(owner=user).count() == 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL concurrency proof")
def test_postgres_same_uuid_concurrent_upload_reserves_one_import(tmp_path, ready_backend):
    _, patient, user = api_context()
    import_id = uuid.uuid4()
    url, _, key = request_payload(patient.pk, import_id)

    def post_once():
        try:
            local_client = APIClient()
            local_client.force_authenticate(user)
            _url, data, _key = request_payload(patient.pk, import_id)
            return local_client.post(url, data=data, format="multipart", HTTP_IDEMPOTENCY_KEY=key)
        finally:
            connections.close_all()

    with fs_storage(tmp_path), ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _index: post_once(), range(2)))
        replay = post_once()
        confirmation_url = f"{url}{import_id}/confirmation/"

        def confirm_once():
            try:
                local_client = APIClient()
                local_client.force_authenticate(user)
                return local_client.put(
                    confirmation_url,
                    {
                        "revision": 1,
                        "patient_confirmed": True,
                        "pages_confirmed": True,
                        "dates_reviewed": True,
                    },
                    format="json",
                )
            finally:
                connections.close_all()

        confirmations = list(executor.map(lambda _index: confirm_once(), range(2)))
        confirmation_replay = confirm_once()

    assert sorted(response.status_code for response in responses) == [202, 409]
    assert replay.status_code == 200
    assert DocumentImport.objects.filter(owner=user, import_uuid=import_id).count() == 1
    assert PrescriptionDocumentAsset.objects.filter(
        document_import__owner=user, role=PrescriptionDocumentAsset.Role.ORIGINAL
    ).count() == 1
    assert sorted(response.status_code for response in confirmations) == [200, 201]
    assert confirmation_replay.status_code == 200
    assert len({response.data["prescription_id"] for response in confirmations}) == 1
    assert Prescription.objects.filter(patient=patient).count() == 1
