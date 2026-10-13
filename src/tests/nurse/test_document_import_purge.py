import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from nurse.models import (
    DocumentImport,
    DocumentImportState,
    Nurse,
    Patient,
    Prescription,
    PrescriptionDocumentAsset,
    PrescriptionDocumentVersion,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def synthetic_fs_storage(tmp_path):
    with override_settings(
        STORAGES={
            "default": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": str(tmp_path)},
            },
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            },
        }
    ):
        yield


def make_patient():
    user = User.objects.create_user(username=f"synthetic-purge-{uuid.uuid4()}")
    nurse = Nurse.objects.create(user=user)
    patient = Patient.objects.create(firstname="Fictif", lastname="Purge")
    nurse.patients.add(patient)
    return user, patient


def test_expired_and_abandoned_staging_is_purged_with_orphans_but_not_confirmed(
    synthetic_fs_storage,
):
    user, patient = make_patient()
    expired = DocumentImport.objects.create(
        owner=user,
        patient=patient,
        import_uuid=uuid.uuid4(),
        state=DocumentImportState.READY,
        metadata={
            "prescribing_doctor": "",
            "start_date": None,
            "end_date": None,
            "source_kind": "image_import",
            "pages": [],
        },
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    expired_key = f"private/document-imports/{expired.pk}/original/expired.png"
    default_storage.save(expired_key, ContentFile(b"synthetic-expired"))
    PrescriptionDocumentAsset.objects.create(
        document_import=expired,
        role=PrescriptionDocumentAsset.Role.ORIGINAL,
        original_index=0,
        storage_key=expired_key,
        mime_type="image/png",
        byte_size=16,
        page_count=1,
        sha256="1" * 64,
    )
    orphan_key = f"private/document-imports/{expired.pk}/original/orphan.png"
    default_storage.save(orphan_key, ContentFile(b"synthetic-orphan"))

    abandoned = DocumentImport.objects.create(
        owner=user,
        patient=patient,
        import_uuid=uuid.uuid4(),
        state=DocumentImportState.ABANDONED,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    abandoned_key = f"private/document-imports/{abandoned.pk}/original/abandoned.png"
    default_storage.save(abandoned_key, ContentFile(b"synthetic-abandoned"))

    archived = DocumentImport.objects.create(
        owner=user,
        patient=patient,
        import_uuid=uuid.uuid4(),
        state=DocumentImportState.CONFIRMED,
        expires_at=timezone.now() - timedelta(days=1),
    )
    prescription = Prescription.objects.create(
        patient=patient,
        prescribing_doctor="",
        start_date=None,
        end_date=None,
        photo_prescription="",
    )
    archived.prescription = prescription
    archived.save(update_fields=["prescription"])
    version = PrescriptionDocumentVersion.objects.create(
        prescription=prescription,
        document_import=archived,
        version=1,
        prescribing_doctor="",
        start_date=None,
        end_date=None,
    )
    confirmed_key = f"private/document-imports/{archived.pk}/original/confirmed.pdf"
    default_storage.save(confirmed_key, ContentFile(b"synthetic-confirmed"))
    PrescriptionDocumentAsset.objects.create(
        document_import=archived,
        document_version=version,
        role=PrescriptionDocumentAsset.Role.ORIGINAL,
        original_index=0,
        storage_key=confirmed_key,
        mime_type="application/pdf",
        byte_size=19,
        page_count=1,
        sha256="2" * 64,
    )

    call_command("purge_document_imports")

    expired.refresh_from_db()
    assert expired.state == DocumentImportState.EXPIRED
    assert expired.metadata is None
    assert not expired.assets.exists()
    assert not default_storage.exists(expired_key)
    assert not default_storage.exists(orphan_key)
    assert not default_storage.exists(abandoned_key)
    assert default_storage.exists(confirmed_key)
    assert archived.assets.count() == 1


def test_confirmed_import_is_never_selected_for_purge(synthetic_fs_storage):
    user, patient = make_patient()
    operation = DocumentImport.objects.create(
        owner=user,
        patient=patient,
        import_uuid=uuid.uuid4(),
        state=DocumentImportState.CONFIRMED,
        expires_at=timezone.now() - timedelta(days=1),
    )
    call_command("purge_document_imports")
    operation.refresh_from_db()
    assert operation.state == DocumentImportState.CONFIRMED
