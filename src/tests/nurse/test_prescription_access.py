from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APIClient

from nurse.models import Nurse, Patient, Prescription
from nurse.serializers import (
    PatientSerializer,
    PrescriptionFileSerializer,
    PrescriptionSerializer,
)

pytestmark = pytest.mark.django_db


def prescription_data(patient):
    return {
        "patient": patient.pk,
        "prescribing_doctor": "Test doctor",
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
    }


def image_file(color="white"):
    data = BytesIO()
    Image.new("RGB", (2, 2), color).save(data, format="PNG")
    return SimpleUploadedFile("test.png", data.getvalue(), content_type="image/png")


@pytest.fixture
def records(tmp_path):
    with override_settings(
        MEDIA_ROOT=tmp_path,
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            },
        },
    ):
        owner = User.objects.create_user(username="owner")
        other = User.objects.create_user(username="other")
        nurse = Nurse.objects.create(user=owner)
        other_nurse = Nurse.objects.create(user=other)
        first = Patient.objects.create(firstname="First", lastname="Test")
        second = Patient.objects.create(firstname="Second", lastname="Test")
        foreign = Patient.objects.create(firstname="Other", lastname="Test")
        nurse.patients.add(first, second)
        other_nurse.patients.add(foreign)
        own = Prescription.objects.create(
            **prescription_data(first) | {"patient": first}
        )
        theirs = Prescription.objects.create(
            **prescription_data(foreign) | {"patient": foreign}
        )
        client = APIClient()
        client.force_authenticate(user=owner)
        yield SimpleNamespace(
            client=client,
            owner=owner,
            nurse=nurse,
            first=first,
            second=second,
            foreign=foreign,
            own=own,
            theirs=theirs,
        )


def write_prescription(records, method, patient, **changes):
    url = "/api/v1/prescription/"
    if method != "post":
        url += f"{records.own.pk}/"
    return getattr(records.client, method)(
        url, {**prescription_data(patient), **changes}, format="json"
    )


@pytest.mark.parametrize("method", ["post", "put", "patch"])
def test_patient_reference_is_limited_to_accessible_records(records, method):
    before = list(Prescription.objects.order_by("id").values())
    response = write_prescription(
        records, method, records.foreign, prescribing_doctor="Unapplied doctor"
    )
    assert response.status_code == 400
    denied = response.data
    assert set(denied) == {"patient"}
    assert denied["patient"][0].code == "does_not_exist"
    assert list(Prescription.objects.order_by("id").values()) == before
    foreign_id = records.foreign.pk
    records.foreign.delete()
    after_delete = list(Prescription.objects.order_by("id").values())
    missing = write_prescription(
        records,
        method,
        SimpleNamespace(pk=foreign_id),
        prescribing_doctor="Unapplied doctor",
    )
    assert missing.status_code == 400
    assert missing.data == denied
    assert list(Prescription.objects.order_by("id").values()) == after_delete
    records.own.refresh_from_db()
    assert records.own.patient_id == records.first.pk


@pytest.mark.parametrize("method", ["post", "put", "patch"])
@pytest.mark.parametrize("shared", [False, True])
def test_writes_preserve_accessible_patient_contract(records, method, shared):
    patient = records.second
    if shared:
        records.nurse.patients.add(records.foreign)
        patient = records.foreign
    response = write_prescription(records, method, patient)
    assert response.status_code == (201 if method == "post" else 200)
    assert response.data["patient"] == patient.pk
    assert response.data["patient_firstname"] == patient.firstname
    assert "photo_prescription" in response.data


def test_partial_update_and_nested_read_preserve_patient(records):
    response = records.client.patch(
        f"/api/v1/prescription/{records.own.pk}/",
        {"prescribing_doctor": "Updated doctor"},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["patient"] == records.first.pk
    nested = PatientSerializer(records.first).data["prescriptions"]
    assert nested[0]["patient"] == records.first.pk
    assert nested[0]["prescribing_doctor"] == "Updated doctor"


@pytest.mark.parametrize(
    "context", [{}, {"request": SimpleNamespace(user=AnonymousUser())}]
)
def test_patient_write_requires_authenticated_context(records, context):
    serializer = PrescriptionSerializer(
        data=prescription_data(records.first), context=context
    )
    assert not serializer.is_valid()
    assert serializer.errors["patient"][0].code == "does_not_exist"


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
@pytest.mark.parametrize("staff", [False, True])
def test_other_prescription_is_unavailable(records, method, staff):
    records.owner.is_staff = staff
    records.owner.save()
    url = f"/api/v1/prescription/{records.theirs.pk}/"
    kwargs = (
        {}
        if method in ("get", "delete")
        else {"data": prescription_data(records.first), "format": "json"}
    )
    response = getattr(records.client, method)(url, **kwargs)
    assert response.status_code == 404
    records.theirs.refresh_from_db()
    assert records.theirs.patient_id == records.foreign.pk
    assert records.theirs.prescribing_doctor == "Test doctor"
    listing = records.client.get("/api/v1/prescription/")
    assert [item["id"] for item in listing.data] == [records.own.pk]


@pytest.mark.parametrize("method", ["put", "patch"])
@pytest.mark.parametrize("target", ["foreign", "missing", "revoked"])
@pytest.mark.parametrize("staff", [False, True])
def test_unavailable_upload_does_not_validate_or_write(records, method, target, staff):
    records.owner.is_staff = staff
    records.owner.save()
    prescription = records.theirs if target == "foreign" else records.own
    prescription.photo_prescription.save("original.png", image_file(), save=True)
    name = prescription.photo_prescription.name
    storage = prescription.photo_prescription.storage
    with storage.open(name, "rb") as original:
        content = original.read()
    if target == "revoked":
        records.nurse.patients.remove(records.first)
    pk = prescription.pk if target != "missing" else 999999
    with patch.object(
        PrescriptionFileSerializer,
        "is_valid",
        autospec=True,
        side_effect=PrescriptionFileSerializer.is_valid,
    ) as validate:
        with patch.object(storage, "save") as save:
            response = getattr(records.client, method)(
                f"/api/v1/prescription/upload/{pk}/",
                {"photo_prescription": SimpleUploadedFile("invalid.png", b"invalid")},
                format="multipart",
            )
    assert response.status_code == 404
    validate.assert_not_called()
    save.assert_not_called()
    prescription.refresh_from_db()
    assert prescription.photo_prescription.name == name
    with storage.open(name, "rb") as unchanged:
        assert unchanged.read() == content


@pytest.mark.parametrize("method", ["put", "patch"])
@pytest.mark.parametrize("shared", [False, True])
def test_upload_and_replacement_preserve_response(records, method, shared):
    prescription = records.own
    if shared:
        records.nurse.patients.add(records.foreign)
        prescription = records.theirs
    url = f"/api/v1/prescription/upload/{prescription.pk}/"
    for color in ("white", "black"):
        upload = image_file(color)
        expected = upload.read()
        upload.seek(0)
        response = getattr(records.client, method)(
            url, {"photo_prescription": upload}, format="multipart"
        )
        assert response.status_code == 200
        assert set(response.data) == {"id", "photo_prescription"}
        assert response.data["id"] == prescription.pk
        assert response.data["photo_prescription"]
        prescription.refresh_from_db()
        with prescription.photo_prescription.open("rb") as saved:
            assert saved.read() == expected


def test_removed_access_prevents_patient_reassignment(records):
    records.nurse.patients.remove(records.second)
    response = write_prescription(records, "patch", records.second)
    assert response.status_code == 400
    records.own.refresh_from_db()
    assert records.own.patient_id == records.first.pk


def test_anonymous_write_is_denied(records):
    client = APIClient()
    assert (
        client.post(
            "/api/v1/prescription/", prescription_data(records.first)
        ).status_code
        == 401
    )
    assert (
        client.patch(
            f"/api/v1/prescription/upload/{records.own.pk}/",
            {"photo_prescription": image_file()},
            format="multipart",
        ).status_code
        == 401
    )
