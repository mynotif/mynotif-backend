import hashlib
import uuid
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.utils import timezone
from rest_framework.test import APIClient

from main.session_services import login
from nurse.document_storage import save_staging
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
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0000004010100c9fe92ef0000000049454e44ae426082"
)


@pytest.fixture
def read_setup(user, settings):
    settings.MEDIA_ROOT = "/tmp/ordopro-document-reads-tests"
    pair = login(user.username, PASSWORD)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
    nurse = Nurse.objects.create(user=user)
    patient = Patient.objects.create(firstname="Patient", lastname="Synthétique")
    nurse.patients.add(patient)
    rx = Prescription.objects.create(
        patient=patient,
        prescribing_doctor="Dr Exemple",
        start_date=None,
        end_date=None,
        photo_prescription="",
    )
    root = f"/api/v3/patients/{patient.pk}/prescriptions/{rx.pk}/"
    return client, nurse, patient, rx, root


def test_historical_manifest_and_binary_are_private_and_stable(read_setup):
    client, _, _, rx, root = read_setup
    rx.photo_prescription.save("synthetic.png", ContentFile(PNG), save=True)
    with patch(
        "nurse.document_read_views.open_private",
        side_effect=AssertionError("v3 storage"),
    ):
        manifest = client.get(root + "documents/")
    assert manifest.status_code == 200
    data = manifest.json()
    assert data["version"] == 1 and len(data["assets"]) == 1
    asset = data["assets"][0]
    assert asset["mime_type"] == "image/png" and asset["role"] == "original"
    assert data["pages"][0]["display_asset_id"] == asset["asset_id"]
    response = client.get(root + f"documents/{asset['asset_id']}/content/")
    assert response.status_code == 200 and response.content == PNG
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Content-Disposition"] == 'attachment; filename="document.png"'
    assert not PrescriptionDocumentAsset.objects.exists()
    with (
        patch(
            "nurse.document_read_views.default_storage.open",
            side_effect=AssertionError("HEAD opened storage"),
        ),
        patch(
            "nurse.document_read_views.default_storage.size",
            side_effect=AssertionError("HEAD read storage metadata"),
        ),
    ):
        head = client.head(root + f"documents/{asset['asset_id']}/content/")
    assert head.status_code == 200 and head.content == b""
    assert head["Content-Type"] == response["Content-Type"]
    assert head["Content-Disposition"] == response["Content-Disposition"]
    assert head["Content-Length"] == "0"
    assert head["X-Content-Type-Options"] == "nosniff"
    scoped = client.options(root + f"documents/{asset['asset_id']}/content/")
    assert scoped.status_code == 200
    assert scoped.json() == {"allowed_methods": "GET, HEAD, OPTIONS"}
    with patch(
        "nurse.document_read_views.default_storage.open",
        side_effect=AssertionError("wrong asset opened storage"),
    ):
        wrong_options = client.options(
            root + "documents/00000000-0000-5000-8000-000000000000/content/"
        )
        wrong_asset = client.get(
            root + "documents/00000000-0000-5000-8000-000000000000/content/"
        )
    assert wrong_options.status_code == 404
    assert wrong_asset.status_code == 404


def test_confirmed_document_manifest_maps_source_and_derivative(read_setup):
    client, nurse, patient, rx, root = read_setup
    owner = User.objects.create_user(username="owner-doc@example.invalid")
    operation = DocumentImport.objects.create(
        owner=owner,
        patient=patient,
        import_uuid=uuid.uuid4(),
        state=DocumentImportState.CONFIRMED,
        metadata={
            "pages": [
                {
                    "asset_index": 0,
                    "source_page": 1,
                    "rotation": 90,
                    "crop": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5},
                }
            ]
        },
        prescription=rx,
        expires_at=timezone.now() + timedelta(days=1),
    )
    version = PrescriptionDocumentVersion.objects.create(
        prescription=rx,
        document_import=operation,
        version=1,
        prescribing_doctor="Dr Exemple",
        start_date=None,
        end_date=None,
    )
    for role, payload in (("original", PNG), ("derived", PNG)):
        key = save_staging(operation.pk, role, payload, "png")
        source_asset = (
            PrescriptionDocumentAsset.objects.get(
                document_import=operation, role="original"
            )
            if role == "derived"
            else None
        )
        asset = PrescriptionDocumentAsset.objects.create(
            document_import=operation,
            document_version=version,
            role=role,
            original_index=0 if role == "original" else None,
            source_asset=source_asset,
            storage_key=key,
            mime_type="image/png",
            byte_size=len(payload),
            page_count=1,
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        if role == "original":
            original = asset
    derived = PrescriptionDocumentAsset.objects.get(role="derived")
    manifest = client.get(root + "documents/")
    assert manifest.status_code == 200
    body = manifest.json()
    assert body["version"] == 1 and len(body["assets"]) == 2
    assert body["pages"] == [
        {
            "source_asset_id": str(original.pk),
            "source_page": 1,
            "display_asset_id": str(derived.pk),
            "display_page": 1,
            "rotation": 90,
            "crop": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5},
        }
    ]
    content = client.get(root + f"documents/{original.pk}/content/")
    assert content.status_code == 200 and b"".join(content.streaming_content) == PNG
    with patch(
        "nurse.document_read_views.open_private",
        side_effect=AssertionError("HEAD opened storage"),
    ):
        content_head = client.head(root + f"documents/{original.pk}/content/")
    assert content_head.status_code == 200 and content_head.content == b""
    assert content_head["Content-Type"] == content["Content-Type"]
    assert content_head["Content-Length"] == content["Content-Length"]
    assert content_head["Content-Disposition"] == content["Content-Disposition"]


def test_reads_scope_foreign_objects_and_options_head_without_storage(read_setup, user):
    client, _, patient, rx, root = read_setup
    other_nurse = Nurse.objects.create(
        user=User.objects.create_user(username="other@example.invalid")
    )
    foreign = Patient.objects.create(firstname="Autre", lastname="Patient")
    other_nurse.patients.add(foreign)
    foreign_rx = Prescription.objects.create(
        patient=foreign, prescribing_doctor="Secret", photo_prescription=""
    )
    denied = client.get(f"/api/v3/patients/{foreign.pk}/prescriptions/")
    assert denied.status_code == 404 and denied.json()["fields"] == {}
    assert (
        client.get(
            f"/api/v3/patients/{patient.pk}/prescriptions/{foreign_rx.pk}/"
        ).status_code
        == 404
    )
    listed = client.get(f"/api/v3/patients/{patient.pk}/prescriptions/?page=1")
    assert (
        listed.status_code == 200 and listed.json()["results"][0]["start_date"] is None
    )
    options = client.options(root + "documents/")
    assert options.status_code == 200
    with patch(
        "nurse.document_read_views._legacy_asset",
        side_effect=AssertionError("storage opened"),
    ):
        head = client.head(root + "documents/")
    assert head.status_code == 200 and head.content == b""
    assert (
        client.head(
            root + "documents/00000000-0000-5000-8000-000000000000/content/"
        ).status_code
        == 404
    )


def test_list_sort_places_unknown_end_dates_after_known_dates(read_setup):
    client, _, patient, _, _ = read_setup
    dated = Prescription.objects.create(
        patient=patient,
        prescribing_doctor="Autre",
        start_date=None,
        end_date=date(2030, 1, 1),
        photo_prescription="",
    )
    newer_unknown = Prescription.objects.create(
        patient=patient,
        prescribing_doctor="Encore",
        start_date=None,
        end_date=None,
        photo_prescription="",
    )
    response = client.get(f"/api/v3/patients/{patient.pk}/prescriptions/?page_size=2")
    assert response.status_code == 200
    assert response.json()["count"] == 3
    assert [row["id"] for row in response.json()["results"]] == [
        dated.pk,
        newer_unknown.pk,
    ]
    assert response.json()["results"][1]["start_date"] is None


def test_openapi_read_routes_have_only_contract_methods():
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator().get_schema(request=None, public=True)
    expected = {
        "/api/v3/patients/{patient_id}/prescriptions/": {"get"},
        "/api/v3/patients/{patient_id}/prescriptions/{prescription_id}/": {"get"},
        "/api/v3/patients/{patient_id}/prescriptions/{prescription_id}/documents/": {
            "get"
        },
        "/api/v3/patients/{patient_id}/prescriptions/{prescription_id}/documents/{asset_id}/content/": {
            "get"
        },
    }
    for path, methods in expected.items():
        assert {
            method
            for method in schema["paths"][path]
            if method in {"get", "head", "options"}
        } == methods
    summary = schema["components"]["schemas"]["PrescriptionSummary"]
    manifest = schema["components"]["schemas"]["DocumentManifest"]
    error = schema["components"]["schemas"]["DocumentReadError"]
    assert summary["additionalProperties"] is False
    assert set(summary["properties"]) == {
        "id",
        "patient_id",
        "prescribing_doctor",
        "start_date",
        "end_date",
    }
    assert manifest["additionalProperties"] is False
    assert {"version", "assets", "pages"} == set(manifest["properties"])
    assert error["additionalProperties"] is False
    assert set(error["properties"]) == {"code", "message", "fields"}
