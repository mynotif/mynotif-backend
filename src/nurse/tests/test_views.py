from datetime import date
from pathlib import Path
from unittest import mock

import boto3
import pytest
import rest_framework
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls.base import reverse_lazy
from freezegun import freeze_time
from moto import mock_aws
from rest_framework import status
from rest_framework.test import APIClient

from nurse.models import Nurse, Patient, Prescription, UserOneSignalProfile

USERNAME = "username1"
PASSWORD = "password1"


def patch_notify():
    return mock.patch("nurse.views.notify")


@pytest.fixture
def user(db):
    """Creates and yields a new user."""
    user = User.objects.create(username=USERNAME)
    user.set_password(PASSWORD)
    user.save()
    yield user
    user.delete()


@pytest.fixture
def staff_user(user):
    """Creates and yields a staff user."""
    user.is_staff = True
    user.save()
    yield user


def authenticate_client_with_token(client, username, password):
    """Authenticates a client using a token and returns it."""
    # void previous credentials to avoid "Invalid token" errors
    client.credentials()
    response = client.post(
        reverse_lazy("api_token_auth"),
        {"username": username, "password": password},
        format="json",
    )
    token = response.data["token"]
    client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
    return client


@pytest.fixture
def authenticated_client(user):
    """Authenticates the user via token and yields it."""
    client = APIClient()
    client = authenticate_client_with_token(client, user.username, PASSWORD)
    yield client
    # invalidates credentials
    client.credentials()


@pytest.fixture
def client(authenticated_client):
    return authenticated_client


@pytest.fixture
def staff_client(staff_user):
    """Authenticates the staff user via token and yields it."""
    client = APIClient()
    client = authenticate_client_with_token(client, staff_user.username, PASSWORD)
    yield client
    # invalidates credentials
    client.credentials()


def attach_prescription(prescription, user):
    patient, _ = Patient.objects.get_or_create(**patient_data)
    nurse, _ = Nurse.objects.get_or_create(user=user)
    nurse.patients.add(patient)
    prescription.patient = patient
    prescription.save()
    return patient, prescription, nurse


def get_test_image():
    image_path = (
        Path(rest_framework.__file__).resolve().parent
        / "static/rest_framework/img/glyphicons-halflings.png"
    )
    image = open(image_path, "rb")
    return SimpleUploadedFile(image.name, image.read())


@pytest.fixture
def s3_mock():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="mynotif-prescription")
        yield


prescription_data = {
    "prescribing_doctor": "Dr Leen",
    "start_date": "2022-07-15",
    "end_date": "2022-07-31",
}

patient_data = {
    "firstname": "John",
    "lastname": "Leen",
    "street": "3 place du cerdan",
    "zip_code": "95400",
    "city": "courdimanche",
    "phone": "0602015454",
    "health_card_number": "12345678910",
    "ss_provider_code": "123456789",
    "birthday": "2023-08-15",
}


@pytest.mark.django_db
class TestPatient:
    url = reverse_lazy("patient-list")
    data = patient_data

    def test_endpoint_patient(self):
        assert self.url == "/patient/"

    def test_create_patient(self, user, client):
        """Creating a patient should link it with the authenticated nurse."""
        response = client.post(self.url, self.data, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert Patient.objects.count() == 1
        patient = Patient.objects.get(phone="0602015454")
        assert patient.firstname == "John"
        assert patient.lastname == "Leen"
        assert patient.street == "3 place du cerdan"
        assert patient.zip_code == "95400"
        assert patient.city == "courdimanche"
        assert patient.health_card_number == "12345678910"
        assert patient.ss_provider_code == "123456789"
        assert patient.birthday.strftime("%Y-%m-%d") == "2023-08-15"
        # the patient is linked to the authenticated nurse
        nurse_set = patient.nurse_set
        assert nurse_set.count() == 1
        nurse_set.all().get().user == user

    def test_create_minimal_patient(self, client):
        """It should be possible to create a patient with only his fullname."""
        data = {
            "firstname": "John",
            "lastname": "Leen",
        }
        assert Patient.objects.count() == 0
        response = client.post(self.url, data, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert Patient.objects.count() == 1
        patient = Patient.objects.get()
        assert patient.firstname == "John"
        assert patient.lastname == "Leen"
        assert patient.phone == ""
        assert patient.street == ""
        assert patient.zip_code == ""
        assert patient.city == ""
        assert patient.health_card_number == ""
        assert patient.ss_provider_code == ""
        assert patient.birthday is None

    @freeze_time("2022-08-11")
    @override_settings(AWS_ACCESS_KEY_ID="testing")
    def test_patient_list(self, user, client):
        patient = Patient.objects.create(**self.data)
        nurse, _ = Nurse.objects.get_or_create(user=user)
        patient.nurse_set.add(nurse)

        # Create 2 prescriptions for the patient
        prescriptions = [
            Prescription.objects.create(
                **{
                    **prescription_data,
                    **{
                        "patient": patient,
                        "start_date": "2022-08-01",
                        "end_date": "2022-08-10",
                    },
                }
            ),
            Prescription.objects.create(
                **{
                    **prescription_data,
                    **{
                        "patient": patient,
                        "start_date": "2022-08-10",
                        "end_date": "2022-08-20",
                    },
                }
            ),
        ]

        response = client.get(self.url)
        assert response.status_code == status.HTTP_200_OK

        expected_data = {
            "id": 1,
            "firstname": "John",
            "lastname": "Leen",
            "street": "3 place du cerdan",
            "zip_code": "95400",
            "city": "courdimanche",
            "phone": "0602015454",
            "health_card_number": "12345678910",
            "ss_provider_code": "123456789",
            "birthday": "2023-08-15",
            "prescriptions": [
                {
                    "id": prescriptions[1].id,
                    "patient": 1,
                    "prescribing_doctor": "Dr Leen",
                    "start_date": "2022-08-10",
                    "end_date": "2022-08-20",
                    "photo_prescription": None,
                    "is_valid": True,
                },
                {
                    "id": prescriptions[0].id,
                    "patient": 1,
                    "prescribing_doctor": "Dr Leen",
                    "start_date": "2022-08-01",
                    "end_date": "2022-08-10",
                    "photo_prescription": None,
                    "is_valid": False,
                },
            ],
        }

        assert response.json() == [expected_data]

    def test_patient_list_401(self):
        """The endpoint should be under authentication."""
        response = APIClient().get(self.url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_patient_detail(self, user, client):
        patient = Patient.objects.create(**self.data)
        nurse, _ = Nurse.objects.get_or_create(user=user)
        patient.nurse_set.add(nurse)
        response = client.get(reverse_lazy("patient-detail", kwargs={"pk": 1}))
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "id": 1,
            "firstname": "John",
            "lastname": "Leen",
            "street": "3 place du cerdan",
            "zip_code": "95400",
            "city": "courdimanche",
            "phone": "0602015454",
            "health_card_number": "12345678910",
            "ss_provider_code": "123456789",
            "birthday": "2023-08-15",
            "prescriptions": [],
        }

    def test_patient_delete(self, user, client):
        patient = Patient.objects.create(**self.data)
        nurse, _ = Nurse.objects.get_or_create(user=user)
        patient.nurse_set.add(nurse)
        response = client.delete(reverse_lazy("patient-detail", kwargs={"pk": 1}))
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert response.data is None
        assert Patient.objects.count() == 0


@pytest.mark.django_db
class TestPrescription:
    url = reverse_lazy("prescription-list")
    data = prescription_data

    def test_endpoint(self):
        assert self.url == "/prescription/"

    # TODO: prescription should only be mapped to a patient's nurse
    def test_create_prescription(self, client):
        response = client.post(self.url, self.data, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert Prescription.objects.count() == 1
        prescription = Prescription.objects.get()
        assert prescription.prescribing_doctor == "Dr Leen"
        assert prescription.start_date == date(2022, 7, 15)
        assert prescription.end_date == date(2022, 7, 31)
        assert prescription.photo_prescription.name == ""

    @freeze_time("2022-08-11")
    def test_prescription_list(self, user, client):
        # creating a prescription with no nurse attached
        prescription = Prescription.objects.create(**self.data)
        response = client.get(self.url)
        assert response.status_code == status.HTTP_200_OK
        # the prescription isn't attached to a nurse
        assert response.json() == []
        # let's link patient, nurse and prescription together
        patient, _, _ = attach_prescription(prescription, user)
        response = client.get(self.url)
        assert response.status_code == status.HTTP_200_OK
        # the nurse should see the attached prescription
        assert response.json() == [
            {
                "id": 1,
                "prescribing_doctor": "Dr Leen",
                "start_date": "2022-07-15",
                "end_date": "2022-07-31",
                "photo_prescription": None,
                "patient": patient.id,
                "is_valid": False,
            }
        ]

    def test_prescription_list_401(self):
        """The endpoint should be under authentication."""
        response = APIClient().get(self.url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @freeze_time("2022-07-20")
    def test_prescription_detail(self, user, client):
        prescription = Prescription.objects.create(**self.data)
        response = client.get(reverse_lazy("prescription-detail", kwargs={"pk": 1}))
        # the prescription/patient is not linked to the logged nurse
        assert response.status_code == status.HTTP_404_NOT_FOUND
        # let's link patient, nurse and prescription together
        patient, _, _ = attach_prescription(prescription, user)
        response = client.get(reverse_lazy("prescription-detail", kwargs={"pk": 1}))
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "id": 1,
            "prescribing_doctor": "Dr Leen",
            "start_date": "2022-07-15",
            "end_date": "2022-07-31",
            "photo_prescription": None,
            "patient": patient.id,
            "is_valid": True,
        }

    def test_prescription_delete(self, user, client):
        prescription = Prescription.objects.create(**self.data)
        response = client.delete(
            reverse_lazy("prescription-detail", kwargs={"pk": prescription.id})
        )
        # the prescription/patient is not linked to the logged nurse
        assert response.status_code == status.HTTP_404_NOT_FOUND
        # let's link patient, nurse and prescription together
        patient, _, _ = attach_prescription(prescription, user)
        response = client.delete(
            reverse_lazy("prescription-detail", kwargs={"pk": prescription.id})
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert response.data is None
        assert Prescription.objects.count() == 0

    # TODO: only allow to upload to prescription we own (and test that), refs #64 & #67
    # TODO: needed?
    @override_settings(AWS_ACCESS_KEY_ID="testing")
    def test_prescription_upload(self, s3_mock, client):
        prescription = Prescription.objects.create(**self.data)
        assert prescription.photo_prescription.name == ""
        with pytest.raises(
            ValueError,
            match="The 'photo_prescription' attribute has no file associated with it.",
        ):
            prescription.photo_prescription.file
        data = {
            "photo_prescription": get_test_image(),
        }
        # patch should also be available
        response = client.put(
            reverse_lazy("prescription-upload", kwargs={"pk": prescription.id}), data
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "id": 1,
            "photo_prescription": mock.ANY,
        }
        assert response.json()["photo_prescription"].startswith(
            "https://mynotif-prescription.s3.amazonaws.com"
            "/prescriptions/glyphicons-halflings.png"
        )
        prescription.refresh_from_db()
        assert prescription.photo_prescription.name.endswith(get_test_image().name)
        # makes sure other fields didn't get overwritten
        assert prescription.prescribing_doctor == "Dr Leen"


@pytest.mark.django_db
class TestNurse:
    url = reverse_lazy("nurse-list")
    data = {
        "user": 1,
        "phone": "0134643232",
        "address": "3 rue de pontoise",
        "zip_code": "95300",
        "city": "Pontoise",
    }

    def test_endpoint(self):
        assert self.url == "/nurse/"

    def test_create_nurse(self, user, client):
        response = client.post(self.url, self.data, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert response.json() == {
            **self.data,
            **{
                "id": 1,
                "patients": [],
            },
        }
        assert response.status_code == status.HTTP_201_CREATED
        nurse = Nurse.objects.get(phone="0134643232")
        assert Nurse.objects.count() == 1
        assert nurse.user == user
        assert nurse.address == "3 rue de pontoise"
        assert nurse.zip_code == "95300"
        assert nurse.city == "Pontoise"

    def test_nurse_list(self, user, client):
        Nurse.objects.create(
            user=user,
            phone="0134643232",
            address="3 rue de pontoise",
            zip_code="95300",
            city="Pontoise",
        )
        response = client.get(self.url)
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == [
            {
                "id": 1,
                "user": 1,
                "patients": [],
                "phone": "0134643232",
                "address": "3 rue de pontoise",
                "zip_code": "95300",
                "city": "Pontoise",
            }
        ]

    def test_nurse_list_401(self):
        """The endpoint should be under authentication."""
        response = APIClient().get(self.url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_nurse_detail(self, user, client):
        user = User.objects.get()
        Nurse.objects.create(
            user=user,
            phone="0134643232",
            address="3 rue de pontoise",
            zip_code="95300",
            city="Pontoise",
        )
        response = client.get(reverse_lazy("nurse-detail", kwargs={"pk": 1}))
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "id": 1,
            "user": 1,
            "patients": [],
            "phone": "0134643232",
            "address": "3 rue de pontoise",
            "zip_code": "95300",
            "city": "Pontoise",
        }


@pytest.mark.django_db
class TestUser:
    url = reverse_lazy("user-list")
    data = {
        "username": "@Issa",
        "email": "issa_test@test.com",
        "password": "password123!@",
    }

    def test_endpoint(self):
        assert self.url == "/user/"

    def test_create_user(self, client):
        """The user creation is via another a different endpoint (/account/register)."""
        response = APIClient().post(self.url, self.data, format="json")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        # it should be no way to create user via this endpoint
        # even being authenticated
        response = client.post(self.url, self.data, format="json")
        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    def test_list_user(self, user, client):
        """Listing user should only return self."""
        expected_response = [
            {
                "id": 1,
                "username": user.username,
                "first_name": "",
                "last_name": "",
                "email": "",
                "is_staff": False,
                "nurse": {
                    "address": "",
                    "city": "",
                    "id": 1,
                    "patients": [],
                    "phone": "",
                    "user": 1,
                    "zip_code": "",
                },
            }
        ]
        response = client.get(self.url)
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == expected_response
        assert User.objects.count() == 1
        User.objects.create(username="another-user")
        response = client.get(self.url)
        assert response.status_code == status.HTTP_200_OK
        # TODO: this is a bug, we shouldn't be able to list other users
        assert response.json() != expected_response

    def test_detail_user(self, user, client):
        response = client.get(reverse_lazy("user-detail", kwargs={"pk": None}))
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "id": 1,
            "username": user.username,
            "first_name": "",
            "last_name": "",
            "email": "",
            "is_staff": False,
            "nurse": {
                "id": 1,
                "address": "",
                "city": "",
                "patients": [],
                "phone": "",
                "user": 1,
                "zip_code": "",
            },
        }

    @pytest.mark.parametrize(
        "action",
        [
            # get
            (lambda client, path, data=None, format=None: client.get(path)),
            # put
            (
                lambda client, path, data=None, format=None: client.put(
                    path, data, format
                )
            ),
            # patch
            (
                lambda client, path, data=None, format=None: client.patch(
                    path, data, format
                )
            ),
            # delete
            (lambda client, path, data=None, format=None: client.delete(path)),
        ],
    )
    def test_pk_permission_denied(self, action, client):
        """We can only view/update/delete self user by using `pk=<not-a-number>`"""
        response = action(
            client,
            # using a number for the pk would mean we're trying to access CRUD
            # operations on a specific user which is forbidden
            reverse_lazy("user-detail", kwargs={"pk": 1}),
            self.data,
            format="json",
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert (
            response.data["detail"]
            == "You do not have permission to perform this action."
        )

    def test_update_user(self, client):
        data = {**self.data, "first_name": "Firstname1", "last_name": "Lastname 1"}
        response = client.put(
            reverse_lazy("user-detail", kwargs={"pk": None}), data, format="json"
        )
        assert response.status_code == status.HTTP_200_OK
        data.pop("password")
        # TODO: first_name and last_name not getting updated, this could be a feature
        # since we're probably using profile but it should be better documented or fixed
        assert response.json() == {
            **data,
            "id": 1,
            "is_staff": False,
            "nurse": {
                "id": 1,
                "address": "",
                "city": "",
                "patients": [],
                "phone": "",
                "user": 1,
                "zip_code": "",
            },
        }

    def test_partial_update_user(self, user, client):
        """Using a patch for a partial update (not all fields)."""
        data = {"first_name": "Firstname1", "last_name": "Lastname 1"}
        response = client.patch(
            reverse_lazy("user-detail", kwargs={"pk": None}), data, format="json"
        )
        assert response.status_code == status.HTTP_200_OK
        # TODO: same bug as test_update_user above
        assert response.json() == {
            "id": 1,
            "username": user.username,
            "email": "",
            "is_staff": False,
            **data,
            "nurse": {
                "id": 1,
                "address": "",
                "city": "",
                "patients": [],
                "phone": "",
                "user": 1,
                "zip_code": "",
            },
        }

    def test_delete_user(self, client):
        response = client.delete(reverse_lazy("user-detail", kwargs={"pk": None}))
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert response.data is None
        assert User.objects.count() == 0


@pytest.mark.django_db
class TestAccountRegister:
    client = APIClient()
    url = reverse_lazy("register")
    username = {"username": USERNAME}
    password = {"password": PASSWORD}
    data = {**username, **password}

    def test_url(self):
        assert self.url == "/account/register"

    @freeze_time("2021-01-16 16:00:00")
    def test_create(self):
        assert User.objects.filter(**self.username).count() == 0
        response = self.client.post(self.url, self.data, format="json")
        assert response.json() == {
            "id": 1,
            "username": USERNAME,
            "first_name": "",
            "last_name": "",
            "email": "",
            "is_staff": False,
            "nurse": {
                "id": 1,
                "address": "",
                "city": "",
                "patients": [],
                "phone": "",
                "user": 1,
                "zip_code": "",
            },
        }
        assert response.status_code == status.HTTP_201_CREATED
        users = User.objects.filter(**self.username)
        assert users.count() == 1
        user = users.get()
        assert user.check_password(self.password["password"]) is True
        assert user.nurse is not None

    def test_create_already_exists(self):
        assert User.objects.filter(**self.username).count() == 0
        response = self.client.post(self.url, self.data, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert User.objects.filter(**self.username).count() == 1
        response = self.client.post(self.url, self.data, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert (
            response.data["username"][0] == "A user with that username already exists."
        )
        assert User.objects.filter(**self.username).count() == 1

    def test_get(self):
        """It's not allowed to list all users."""
        response = self.client.get(self.url)
        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        assert response.data == {"detail": 'Method "GET" not allowed.'}

    def test_auth_ok(self):
        """Note this isn't a REST endpoint."""
        url = reverse_lazy("rest_framework:login")
        assert url == "/account/login/"
        user = User.objects.create(**self.username)
        user.set_password(self.password["password"])
        user.save()
        response = self.client.post(url, self.data)
        assert response.status_code == status.HTTP_302_FOUND
        # redirecting to the profile page
        assert response.get("Location") == "/accounts/profile/"

    def test_auth_error(self):
        url = reverse_lazy("rest_framework:login")
        response = self.client.post(url, self.data)
        assert response.status_code == status.HTTP_200_OK
        assert (
            "Please enter a correct username and password" in response.content.decode()
        )


@pytest.mark.django_db
class TestProfile:
    url = "/profile/"

    def test_endpoint(self):
        assert self.url == reverse_lazy("profile")

    def test_get(self, client):
        response = client.get(self.url)
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "id": 1,
            "username": USERNAME,
            "first_name": "",
            "last_name": "",
            "email": "",
            "is_staff": False,
            "nurse": {
                "address": "",
                "city": "",
                "id": 1,
                "patients": [],
                "phone": "",
                "user": 1,
                "zip_code": "",
            },
        }


@pytest.mark.django_db
class TestAdminNotificationView:
    url = reverse_lazy("notify")

    def test_endpoint_patient(self):
        assert self.url == "/notify/"

    def test_post(self, user, staff_client):
        """Posting to the endpoint should send notifications."""
        with patch_notify() as mock_notify:
            response = staff_client.post(self.url, {}, format="json")
        assert mock_notify.call_args_list == [mock.call()]
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert response.data is None

    def test_post_unauthenticated(self, user, client):
        """Unauthenticated clients aren't allowed."""
        with patch_notify() as mock_notify:
            response = APIClient().post(self.url, {}, format="json")
        assert mock_notify.call_args_list == []
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json() == {
            "detail": "Authentication credentials were not provided."
        }

    def test_post_only_staff(self, user, client):
        """Only staff users are allowed."""
        with patch_notify() as mock_notify:
            response = client.post(self.url, {}, format="json")
        assert mock_notify.call_args_list == []
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json() == {
            "detail": "You do not have permission to perform this action."
        }


@pytest.fixture
def one_signal_profile(user):
    userDetail = UserOneSignalProfile.objects.create(
        user=user, subscription_id="123456789"
    )
    yield userDetail
    userDetail.delete()


@pytest.fixture
def one_signal_profile2(another_user):
    oneSignal = UserOneSignalProfile.objects.create(
        user=another_user, subscription_id="987654321"
    )
    yield oneSignal
    oneSignal.delete()


@pytest.fixture
def another_user(db):
    user = User.objects.create_user(
        username="username2", email="user2@example.com", password="testpass123"
    )
    yield user
    user.delete()


@pytest.mark.django_db
class TestUserOneSignalProfileView:
    url = reverse_lazy("useronesignalprofile-list")

    def test_endpoint_onesignal(self):
        assert self.url == "/onesignal/"

    def test_create_onesignal(self, client, user):
        data = {"subscription_id": "1233456789"}
        client.force_authenticate(user=user)
        assert UserOneSignalProfile.objects.count() == 0
        response = client.post(self.url, data, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert UserOneSignalProfile.objects.count() == 1
        profile = UserOneSignalProfile.objects.first()
        assert response.data["user"] == user.id
        assert profile.subscription_id == data["subscription_id"]

    def test_read_onesignal_list(
        self, client, user, one_signal_profile, one_signal_profile2
    ):
        client.force_authenticate(user=user)
        response = client.get(self.url)
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]["subscription_id"] == one_signal_profile.subscription_id
