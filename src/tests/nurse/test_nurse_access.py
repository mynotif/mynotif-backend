from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.urls import reverse
from rest_framework.test import APIClient

from nurse.models import Nurse, Patient, Prescription

pytestmark = pytest.mark.django_db


@pytest.fixture
def accounts():
    users = [User.objects.create_user(username=name) for name in ("alpha", "beta")]
    patients = [
        Patient.objects.create(firstname=name, lastname="Example")
        for name in ("First", "Second", "Third")
    ]
    nurses = [
        Nurse.objects.create(user=user, phone="initial", city="Original")
        for user in (*users, None)
    ]
    for nurse, patient in zip(nurses, patients):
        nurse.patients.add(patient)
    Prescription.objects.create(
        patient=patients[1],
        prescribing_doctor="Example",
        start_date="2026-01-01",
        end_date="2026-12-31",
    )
    client = APIClient()
    client.force_authenticate(users[0])
    return client, users, nurses, patients


def snapshot():
    return (
        list(Nurse.objects.order_by("id").values()),
        list(Nurse.patients.through.objects.order_by("id").values()),
        list(Patient.objects.order_by("id").values()),
        list(Prescription.objects.order_by("id").values()),
    )


@pytest.mark.parametrize("staff", [False, True])
def test_collection_contains_only_current_profile(accounts, staff):
    client, users, nurses, patients = accounts
    users[0].is_staff = staff
    users[0].save()
    response = client.get(reverse("v1:nurse-list"))
    assert response.status_code == 200
    assert [item["id"] for item in response.data] == [nurses[0].id]
    assert response.data[0]["user"] == users[0].id
    assert response.data[0]["patients"] == [patients[0].id]


def test_empty_collection_does_not_create_profile():
    user = User.objects.create_user(username="new")
    client = APIClient()
    client.force_authenticate(user)
    response = client.get(reverse("v1:nurse-list"))
    assert response.status_code == 200
    assert response.data == []
    assert not Nurse.objects.filter(user=user).exists()


@pytest.mark.parametrize("target", [1, 2])
@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
def test_other_and_orphan_profiles_are_not_accessible(accounts, target, method):
    client, users, nurses, patients = accounts
    before = snapshot()
    response = getattr(client, method)(
        reverse("v1:nurse-detail", args=[nurses[target].id]),
        {"user": users[0].id, "patients": [patients[0].id], "phone": "changed"},
        format="json",
    )
    assert response.status_code == 404
    assert snapshot() == before


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
def test_profile_routes_require_authentication(accounts, method):
    _, _, nurses, _ = accounts
    url = (
        reverse("v1:nurse-list")
        if method == "post"
        else reverse("v1:nurse-detail", args=[nurses[0].id])
    )
    before = snapshot()
    response = getattr(APIClient(), method)(url, {}, format="json")
    assert response.status_code == 401
    assert APIClient().get(reverse("v1:nurse-list")).status_code == 401
    assert snapshot() == before


@pytest.mark.parametrize("method", ["put", "patch"])
@pytest.mark.parametrize("replace_with_other", [False, True])
def test_own_contacts_change_without_client_relation_changes(
    accounts, method, replace_with_other
):
    client, users, nurses, patients = accounts
    response = getattr(client, method)(
        reverse("v1:nurse-detail", args=[nurses[0].id]),
        {
            "id": nurses[1].id,
            "user": users[1].id,
            "patients": [patients[1].id] if replace_with_other else [],
            "phone": "123",
            "address": "Test street",
            "zip_code": "12345",
            "city": "Updated",
        },
        format="json",
    )
    assert response.status_code == 200
    nurses[0].refresh_from_db()
    nurses[1].refresh_from_db()
    assert nurses[0].user_id == users[0].id
    assert list(nurses[0].patients.all()) == [patients[0]]
    assert response.data["id"] == nurses[0].id
    assert response.data["user"] == users[0].id
    assert response.data["patients"] == [patients[0].id]
    assert (nurses[0].phone, nurses[0].city) == ("123", "Updated")
    assert (nurses[0].address, nurses[0].zip_code) == ("Test street", "12345")
    assert nurses[1].phone == "initial"
    assert list(nurses[1].patients.all()) == [patients[1]]
    assert Prescription.objects.get().patient_id == patients[1].id


def test_create_profile_uses_current_account_and_ignores_relations(accounts):
    client, users, nurses, patients = accounts
    nurses[0].delete()
    response = client.post(
        reverse("v1:nurse-list"),
        {
            "id": nurses[1].id,
            "user": users[1].id,
            "patients": [patients[1].id],
            "phone": "123",
        },
        format="json",
    )
    assert response.status_code == 201
    created = Nurse.objects.get(user=users[0])
    assert created.phone == "123"
    assert not created.patients.exists()
    assert response.data["id"] == created.id
    assert response.data["user"] == users[0].id
    assert response.data["patients"] == []
    assert Nurse.objects.filter(user=users[0]).count() == 1
    assert list(nurses[1].patients.all()) == [patients[1]]


def test_duplicate_profile_returns_stable_error_without_overwrite(accounts):
    client, _, _, patients = accounts
    before = snapshot()
    response = client.post(
        reverse("v1:nurse-list"),
        {"phone": "replacement", "patients": [patients[1].id]},
        format="json",
    )
    assert response.status_code == 400
    assert response.data == {"user": ["Nurse profile already exists."]}
    assert snapshot() == before


def test_profile_validates_contact_fields_before_creation(accounts):
    client, users, nurses, _ = accounts
    nurses[0].delete()
    response = client.post(reverse("v1:nurse-list"), {"phone": "x" * 31})
    assert response.status_code == 400
    assert "phone" in response.data
    assert not Nurse.objects.filter(user=users[0]).exists()


def test_existing_result_from_creation_primitive_is_not_overwritten(accounts):
    client, users, nurses, _ = accounts
    nurses[0].delete()

    def another_creation(*, user, defaults):
        assert user == users[0]
        assert defaults == {"phone": "request"}
        return Nurse.objects.create(user=user, phone="already-created"), False

    with patch("nurse.views.Nurse.objects.get_or_create", side_effect=another_creation):
        response = client.post(reverse("v1:nurse-list"), {"phone": "request"})
    assert response.status_code == 400
    assert response.data == {"user": ["Nurse profile already exists."]}
    assert Nurse.objects.filter(user=users[0]).count() == 1
    assert Nurse.objects.get(user=users[0]).phone == "already-created"


def test_unrelated_integrity_error_is_not_reported_as_duplicate(accounts):
    client, _, _, _ = accounts
    with patch("nurse.views.Nurse.objects.get_or_create", side_effect=IntegrityError):
        with pytest.raises(IntegrityError):
            client.post(reverse("v1:nurse-list"), {})


def test_own_profile_can_be_read_and_deleted_without_deleting_patients(accounts):
    client, users, nurses, patients = accounts
    url = reverse("v1:nurse-detail", args=[nurses[0].id])
    assert client.get(url).data["patients"] == [patients[0].id]
    assert client.delete(url).status_code == 204
    assert not Nurse.objects.filter(user=users[0]).exists()
    assert Patient.objects.filter(id=patients[0].id).exists()
    assert Nurse.objects.filter(id=nurses[1].id).exists()
    assert list(nurses[1].patients.all()) == [patients[1]]


def test_patient_creation_and_profile_reads_preserve_own_relationship(accounts):
    client, users, nurses, patients = accounts
    response = client.post(
        reverse("v1:patient-list"),
        {"firstname": "New", "lastname": "Example", "nurse_set": [nurses[1].id]},
        format="json",
    )
    assert response.status_code == 201
    created = Patient.objects.get(id=response.data["id"])
    assert list(created.nurse_set.all()) == [nurses[0]]
    expected = {patients[0].id, created.id}
    for url in (reverse("v1:profile"), reverse("v1:user-detail", args=["current"])):
        response = client.get(url)
        assert response.status_code == 200
        assert response.data["nurse"]["user"] == users[0].id
        assert set(response.data["nurse"]["patients"]) == expected
    for method in ("put", "patch"):
        response = getattr(client, method)(
            reverse("v1:patient-detail", args=[created.id]),
            {
                "firstname": "Updated",
                "lastname": "Example",
                "nurse_set": [nurses[1].id],
            },
            format="json",
        )
        assert response.status_code == 200
        assert list(created.nurse_set.all()) == [nurses[0]]
    assert list(nurses[1].patients.all()) == [patients[1]]
