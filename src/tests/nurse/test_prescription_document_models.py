import sys
import uuid
from datetime import date, timedelta
from types import ModuleType

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import APIException, UnsupportedMediaType
from rest_framework.test import APIClient

from nurse.models import (
    DocumentImport,
    Patient,
    Prescription,
    PrescriptionDocumentAsset,
    PrescriptionDocumentVersion,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def document_records():
    owner = User.objects.create_user(username="synthetic-document-owner")
    patient = Patient.objects.create(firstname="Synthetic", lastname="Patient")
    prescription = Prescription.objects.create(
        patient=patient,
        prescribing_doctor="",
        start_date=None,
        end_date=None,
        photo_prescription="",
    )
    operation = DocumentImport.objects.create(
        owner=owner,
        patient=patient,
        import_uuid=uuid.uuid4(),
        expires_at=timezone.now() + timedelta(hours=24),
    )
    return owner, patient, prescription, operation


def test_import_uuid_is_unique_per_owner_and_not_global(document_records):
    owner, patient, _, operation = document_records
    with pytest.raises(IntegrityError), transaction.atomic():
        DocumentImport.objects.create(
            owner=owner,
            patient=patient,
            import_uuid=operation.import_uuid,
            expires_at=timezone.now() + timedelta(hours=24),
        )

    other_owner = User.objects.create_user(username="synthetic-document-other")
    other_patient = Patient.objects.create(firstname="Synthetic", lastname="Other")
    other = DocumentImport.objects.create(
        owner=other_owner,
        patient=other_patient,
        import_uuid=operation.import_uuid,
        expires_at=timezone.now() + timedelta(hours=24),
    )
    assert other.import_uuid == operation.import_uuid


def test_import_dates_are_nullable_and_versions_are_unique(document_records):
    _, _, prescription, operation = document_records
    version = PrescriptionDocumentVersion.objects.create(
        prescription=prescription,
        document_import=operation,
        version=1,
        prescribing_doctor="",
        start_date=None,
        end_date=None,
    )
    assert version.start_date is None
    assert version.end_date is None
    assert prescription.start_date is None
    assert prescription.end_date is None
    assert not prescription.is_valid()
    assert not prescription.expiring_soon()


def test_asset_requires_source_matching_its_role(document_records):
    _, _, _, operation = document_records
    with pytest.raises(IntegrityError), transaction.atomic():
        PrescriptionDocumentAsset.objects.create(
            document_import=operation,
            role=PrescriptionDocumentAsset.Role.DERIVED,
            original_index=None,
            source_asset=None,
            storage_key="private/synthetic/derived",
            mime_type="image/jpeg",
            byte_size=4,
            page_count=1,
            sha256="0" * 64,
        )


def test_asset_metadata_cannot_be_overwritten(document_records):
    _, _, _, operation = document_records
    asset = PrescriptionDocumentAsset.objects.create(
        document_import=operation,
        role=PrescriptionDocumentAsset.Role.ORIGINAL,
        original_index=0,
        source_asset=None,
        storage_key="private/synthetic/original",
        mime_type="image/jpeg",
        byte_size=4,
        page_count=1,
        sha256="0" * 64,
    )
    asset.storage_key = "private/synthetic/replaced"
    with pytest.raises(ValueError, match="immutable"):
        asset.save()


def test_v3_rejects_legacy_token_authentication():
    owner = User.objects.create_user(username="synthetic-legacy-token-owner")
    token = Token.objects.create(user=owner)
    response = APIClient().get(
        "/api/v3/document-capabilities/",
        HTTP_AUTHORIZATION=f"Token {token.key}",
    )
    assert response.status_code == 401
    assert response.data == {
        "code": "authentication_required",
        "message": "Authentification requise.",
        "fields": {},
    }
    assert response["WWW-Authenticate"] == "Bearer"
    assert response["Cache-Control"] == "private, no-store"
    assert response["Pragma"] == "no-cache"


def test_unimplemented_v3_handler_fails_closed(document_records):
    owner, _, _, _ = document_records
    client = APIClient()
    client.force_authenticate(owner)

    response = client.get("/api/v3/document-capabilities/")

    assert response.status_code == 503
    assert response.data["code"] == "service_unavailable"
    assert response["Cache-Control"] == "private, no-store"
    assert response["Pragma"] == "no-cache"


def test_v3_unexpected_handler_error_has_closed_response_and_redacted_log(
    document_records, monkeypatch, caplog
):
    owner, _, _, _ = document_records
    marker = "SYNTHETIC_PRIVATE_EXCEPTION_MARKER"
    module = ModuleType("nurse.document_import_views")

    def fail(*args, **kwargs):
        raise ValueError(marker)

    module.dispatch = fail
    monkeypatch.setitem(sys.modules, module.__name__, module)
    client = APIClient()
    client.force_authenticate(owner)

    response = client.get("/api/v3/document-capabilities/")

    assert response.status_code == 503
    assert response.data == {
        "code": "service_unavailable",
        "message": "Service indisponible. Réessayez.",
        "fields": {},
    }
    assert marker not in response.content.decode()
    assert marker not in caplog.text
    assert response["Cache-Control"] == "private, no-store"


def test_v3_exception_preserves_stable_contract_code(document_records, monkeypatch):
    owner, _, _, _ = document_records
    module = ModuleType("nurse.document_import_views")

    def parse(*args, **kwargs):
        raise UnsupportedMediaType("SYNTHETIC_UNTRUSTED_CONTENT_TYPE")

    module.dispatch = parse
    monkeypatch.setitem(sys.modules, module.__name__, module)
    client = APIClient()
    client.force_authenticate(owner)

    response = client.generic(
        "POST",
        "/api/v3/document-capabilities/",
        b"synthetic",
        content_type="application/octet-stream",
    )

    assert response.status_code == 415
    assert response.data == {
        "code": "unsupported_media_type",
        "message": "Format de document non pris en charge.",
        "fields": {},
    }


@pytest.mark.parametrize(
    ("status_code", "code", "message"),
    [
        (
            409,
            "idempotency_conflict",
            "Cette clé correspond à un autre document.",
        ),
        (400, "invalid_dates", "Les dates du document sont invalides."),
        (
            503,
            "validation_unavailable",
            "La validation est momentanément indisponible.",
        ),
    ],
)
def test_v3_exception_preserves_only_allowlisted_contract_codes(
    document_records, monkeypatch, status_code, code, message
):
    owner, _, _, _ = document_records
    module = ModuleType("nurse.document_import_views")
    exception_type = type(
        "SyntheticContractError",
        (APIException,),
        {"status_code": status_code},
    )

    def fail(*args, **kwargs):
        raise exception_type(
            {
                "code": code,
                "message": "SYNTHETIC_UNTRUSTED_ERROR_MESSAGE",
                "fields": {"patient": ["SYNTHETIC_PRIVATE_FIELD"]},
            }
        )

    module.dispatch = fail
    monkeypatch.setitem(sys.modules, module.__name__, module)
    client = APIClient()
    client.force_authenticate(owner)

    response = client.get("/api/v3/document-capabilities/")

    assert response.status_code == status_code
    assert response.data == {"code": code, "message": message, "fields": {}}


@pytest.mark.parametrize(
    ("path", "requested_method", "allowed_methods"),
    [
        (
            "/api/v3/patients/1/document-imports/",
            "GET",
            "GET, HEAD, POST, OPTIONS",
        ),
        (
            "/api/v3/patients/1/document-imports/",
            "POST",
            "GET, HEAD, POST, OPTIONS",
        ),
        (
            "/api/v3/patients/1/document-imports/00000000-0000-0000-0000-000000000001/",
            "PATCH",
            "GET, HEAD, PATCH, DELETE, OPTIONS",
        ),
        (
            (
                "/api/v3/patients/1/document-imports/"
                "00000000-0000-0000-0000-000000000001/confirmation/"
            ),
            "HEAD",
            "HEAD, PUT, OPTIONS",
        ),
    ],
)
def test_v3_cors_preflight_requires_auth_and_advertises_route_methods(
    path, requested_method, allowed_methods
):
    headers = {
        "HTTP_ORIGIN": "https://synthetic.invalid",
        "HTTP_ACCESS_CONTROL_REQUEST_METHOD": requested_method,
    }
    with override_settings(CORS_ALLOWED_ORIGINS=["https://synthetic.invalid"]):
        unauthenticated = APIClient().options(path, **headers)
        assert unauthenticated.status_code == 401
        assert unauthenticated.data["code"] == "authentication_required"
        assert unauthenticated["Access-Control-Allow-Origin"] == (
            "https://synthetic.invalid"
        )
        assert requested_method in unauthenticated["Access-Control-Allow-Methods"]

        owner = User.objects.create_user(username="synthetic-cors-owner")
        authenticated = APIClient()
        authenticated.force_authenticate(owner)
        response = authenticated.options(path, **headers)
        assert response.status_code == 200
        assert response["Access-Control-Allow-Methods"] == allowed_methods


@pytest.mark.django_db(transaction=True)
def test_nullable_date_migration_preserves_historical_values():
    migration_from = ("nurse", "0016_prescription_documents")
    migration_to = ("nurse", "0017_nullable_prescription_dates")
    executor = MigrationExecutor(connection)
    executor.migrate([migration_from])
    old_apps = executor.loader.project_state([migration_from]).apps
    patient_model_before = old_apps.get_model("nurse", "Patient")
    prescription_model_before = old_apps.get_model("nurse", "Prescription")
    patient = patient_model_before.objects.create(
        firstname="Synthetic", lastname="Before"
    )
    original_dates = (date(2024, 2, 29), date(2025, 3, 1))
    prescription = prescription_model_before.objects.create(
        patient_id=patient.pk,
        prescribing_doctor="Synthetic",
        start_date=original_dates[0],
        end_date=original_dates[1],
        photo_prescription="private/synthetic/historical.jpg",
    )
    prescription_id = prescription.pk

    try:
        MigrationExecutor(connection).migrate([migration_to])
        new_apps = (
            MigrationExecutor(connection).loader.project_state([migration_to]).apps
        )
        migrated = new_apps.get_model("nurse", "Prescription").objects.get(
            pk=prescription_id
        )
        assert (migrated.start_date, migrated.end_date) == original_dates
        assert migrated.photo_prescription == "private/synthetic/historical.jpg"
    finally:
        MigrationExecutor(connection).migrate(
            MigrationExecutor(connection).loader.graph.leaf_nodes()
        )
