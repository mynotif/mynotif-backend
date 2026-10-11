from unittest.mock import Mock, patch

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from rest_framework.test import APIClient

from nurse.models import Nurse, Patient, Prescription

pytestmark = pytest.mark.django_db


@pytest.fixture
def file_records(tmp_path):
    with override_settings(
        MEDIA_ROOT=tmp_path,
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            },
        },
    ):
        owner = User.objects.create_user(username="file-owner")
        other = User.objects.create_user(username="file-other")
        patient = Patient.objects.create(firstname="Synthetic", lastname="A")
        foreign_patient = Patient.objects.create(firstname="Synthetic", lastname="B")
        Nurse.objects.create(user=owner).patients.add(patient)
        Nurse.objects.create(user=other).patients.add(foreign_patient)
        own = Prescription.objects.create(
            patient=patient,
            prescribing_doctor="Synthetic",
            start_date="2026-01-01",
            end_date="2026-12-31",
            photo_prescription="synthetic/a.png",
        )
        foreign = Prescription.objects.create(
            patient=foreign_patient,
            prescribing_doctor="Synthetic",
            start_date="2026-01-01",
            end_date="2026-12-31",
            photo_prescription="synthetic/b.png",
        )
        client = APIClient()
        client.force_authenticate(owner)
        yield type("Records", (), {"own": own, "theirs": foreign, "client": client})


def test_file_url_checks_access_before_storage(file_records):
    records = file_records
    storage = records.own.photo_prescription.storage
    exists = Mock(return_value=True)
    url = Mock(return_value="https://synthetic.invalid/signed?X-Amz-Expires=300")
    with patch.object(storage, "exists", exists), patch.object(storage, "url", url):
        response = records.client.get(
            f"/api/v1/prescription/{records.own.pk}/file-url/"
        )
        assert response.status_code == 200
        assert response.data["url"] == url.return_value
        assert response.data["expires_in"] == 300
        assert response.data["expires_at"]
        assert response["Cache-Control"] == "private, no-store"
        exists.assert_called_once()
        url.assert_called_once()
        exists.reset_mock()
        url.reset_mock()
        response = records.client.get(
            f"/api/v1/prescription/{records.theirs.pk}/file-url/"
        )
        assert response.status_code == 404
        exists.assert_not_called()
        url.assert_not_called()


def test_legacy_responses_containing_signed_urls_are_not_cacheable(file_records):
    records = file_records
    storage = records.own.photo_prescription.storage
    with patch.object(storage, "url", return_value="/synthetic/signed-url"):
        for path in ("/api/v1/prescription/", "/api/v1/patient/"):
            response = records.client.get(path)
            assert response.status_code == 200
            assert response["Cache-Control"] == "private, no-store"


def test_staff_does_not_bypass_patient_file_scope(file_records):
    records = file_records
    records.client.force_authenticate(
        user=records.theirs.patient.nurse_set.first().user
    )
    staff = records.theirs.patient.nurse_set.first().user
    staff.is_staff = True
    staff.save(update_fields=("is_staff",))
    storage = records.own.photo_prescription.storage
    with patch.object(storage, "exists") as exists:
        response = records.client.get(
            f"/api/v1/prescription/{records.own.pk}/file-url/"
        )
    assert response.status_code == 404
    exists.assert_not_called()


def test_file_missing_and_storage_failure_are_safe(file_records):
    records = file_records
    storage = records.own.photo_prescription.storage
    with patch.object(storage, "exists", return_value=False):
        response = records.client.get(
            f"/api/v1/prescription/{records.own.pk}/file-url/"
        )
    assert response.status_code == 404
    with patch.object(storage, "exists", side_effect=OSError("private storage detail")):
        response = records.client.get(
            f"/api/v1/prescription/{records.own.pk}/file-url/"
        )
    assert response.status_code == 503
    assert "private storage detail" not in str(response.data)


def test_signed_url_ttl_is_bounded(file_records):
    records = file_records
    with override_settings(PRIVATE_FILE_URL_TTL_SECONDS=901):
        from nurse.utils.private_files import signed_file_url

        with pytest.raises(RuntimeError):
            signed_file_url(records.own.photo_prescription)


def test_s3_signed_url_uses_short_query_expiry(settings):
    import boto3
    from moto import mock_aws
    from storages.backends.s3 import S3Storage

    settings.AWS_ACCESS_KEY_ID = "testing"
    settings.AWS_SECRET_ACCESS_KEY = "testing"
    settings.AWS_STORAGE_BUCKET_NAME = "synthetic-prescription-files"
    with mock_aws():
        client = boto3.client("s3", region_name="eu-west-3")
        client.create_bucket(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-west-3"},
        )
        storage = S3Storage(bucket_name=settings.AWS_STORAGE_BUCKET_NAME)
        signed = storage.url("prescriptions/synthetic.png", expire=300)
        assert "X-Amz-Expires=300" in signed
        assert "X-Amz-Signature=" in signed
