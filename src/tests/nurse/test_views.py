from datetime import date
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import boto3
import pytest
import rest_framework
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls.base import reverse_lazy
from freezegun import freeze_time
from moto import mock_aws
from rest_framework import status
from rest_framework.test import APIClient

from nurse.models import Nurse, Patient, Prescription, UserOneSignalProfile
from nurse.utils.constants import FREE_LIMIT_MESSAGE
from payment.models import Subscription
from tests.conftest import (
    EMAIL,
    EMAIL_HOST_USER,
    FIRSTNAME,
    LASTNAME,
    PASSWORD,
    USERNAME,
    authenticate_client_with_token,
)


def patch_notify():
    return mock.patch("nurse.views.notify")


@pytest.fixture
def user2(db):
    """Creates and yields a new user."""
    user = User.objects.create(
        username="username2", first_name=FIRSTNAME, last_name=LASTNAME, email=EMAIL
    )
    user.set_password(PASSWORD)
    user.save()
    yield user
    user.delete()


@pytest.fixture
def client(authenticated_client):
    return authenticated_client


@pytest.fixture
def staff_client(staff_user):
    """Authenticates the staff user via token and yields it."""
    client = APIClient()
    client = authenticate_client_with_token(client, staff_user.email, PASSWORD)
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
    "email_doctor": "dr.a@example.com",
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


@pytest.fixture
def prescription(user):
    patient = Patient.objects.create(**patient_data)
    nurse, _ = Nurse.objects.get_or_create(user=user)
    nurse.patients.add(patient)
    return Prescription.objects.create(patient=patient, **prescription_data)


@pytest.mark.django_db
class TestSendEmailToDoctorView:
    valid_payload = {"additional_info": "This is a valid message."}
    invalid_payload = {"additional_info": "This message contains <invalid> characters."}
    url = reverse_lazy("v1:send-email-to-doctor", kwargs={"pk": 1})

    def test_endpoint(self):
        assert self.url == "/api/v1/prescription/1/send-email/"

    @override_settings(EMAIL_HOST_USER=EMAIL_HOST_USER)
    def test_send_email_to_doctor(self, client, prescription, user):
        response = client.post(self.url, self.valid_payload)
        assert response.status_code == status.HTTP_200_OK
        assert response.data == {"message": "Email sent"}
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [prescription.email_doctor]
        assert mail.outbox[0].from_email == EMAIL_HOST_USER

        # Check the rendered email message
        email = mail.outbox[0]
        assert "Renouveler ordonnance" in email.subject
        assert email.alternatives[0][1] == "text/html"
        assert email.reply_to == [user.email]
        html_body = email.alternatives[0][0]

        assert "<li>Patient : John Leen</li>" in html_body
        assert "<li>Date de naissance : Aug. 15, 2023</li>" in html_body
        assert "<pre>This is a valid message.</pre>" in html_body
        assert "John Doe<br />" in html_body
        assert "Infirmière Libérale" in html_body

    def test_send_email_to_doctor_404(self, client):
        response = client.post(self.url, self.valid_payload)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data == {"detail": "No Prescription matches the given query."}

    def test_doctor_has_no_email(self, client, prescription):
        prescription.email_doctor = ""
        prescription.save()
        response = client.post(self.url, self.valid_payload)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data == {"error": "Doctor has no email"}

    def test_user_without_name(self, client, prescription, user):
        user.first_name = ""
        user.last_name = ""
        user.email = ""
        user.save()
        response = client.post(self.url, self.valid_payload)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data == {
            "error": (
                "Please update your profile with your first and last name and email"
            )
        }

    def test_send_email_to_doctor_500(self, client, prescription):
        with patch(
            "nurse.views.send_mail_with_reply", side_effect=Exception("SMTP Error")
        ):
            response = client.post(self.url, self.valid_payload)
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert response.data == {"error": "SMTP Error"}

    def test_send_email_to_doctor_unauthorized_patient(
        self, client, user2, prescription
    ):
        # Make sure user is a nurse but does not have the patient associated
        nurse = Nurse.objects.create(user=user2)
        # Ensure the nurse is not associated with the patient
        assert not nurse.patients.filter(id=prescription.patient.id).exists()
        # Authenticate as the nurse
        client.force_authenticate(user=user2)
        response = client.post(self.url, self.valid_payload)
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data == {
            "error": "You are not authorized to send emails to this patient"
        }

    def test_send_email_to_doctor_invalid_additional_info(
        self, client, prescription, user
    ):
        response = client.post(self.url, self.invalid_payload)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data == {"additional_info": ["Enter a valid value."]}


@pytest.mark.django_db
class TestPatient:
    url = reverse_lazy("v1:patient-list")
    data = patient_data

    def test_endpoint_patient(self):
        assert self.url == "/api/v1/patient/"

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

    @override_settings(FREE_PATIENT_LIMIT=2)
    def test_non_subscribed_user_cannot_exceed_patient_limit(self, user, client):
        """
        Ensure that a non-subscribed
        user cannot create more patients than the allowed limit.
        """
        LIMIT = settings.FREE_PATIENT_LIMIT
        assert Patient.objects.count() == 0
        data = {
            "firstname": "John",
            "lastname": "Leen",
        }
        for _ in range(LIMIT):
            response = client.post(self.url, data, format="json")
            assert response.status_code == status.HTTP_201_CREATED
        assert Patient.objects.count() == LIMIT
        response = client.post(self.url, data, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data == {"detail": FREE_LIMIT_MESSAGE}

    @override_settings(FREE_PATIENT_LIMIT=2)
    def test_subscribed_user_can_exceed_patient_limit(self, user, client):
        """
        Ensure that a subscribed user
        can create more patient than the allowed limit.
        """
        LIMIT = settings.FREE_PATIENT_LIMIT
        Subscription.objects.create(
            user=user, stripe_subscription_id="session_id_example", active=True
        )
        data = {
            "firstname": "John",
            "lastname": "Leen",
        }
        for _ in range(LIMIT + 1):
            response = client.post(self.url, data, format="json")
            assert response.status_code == status.HTTP_201_CREATED
        assert Patient.objects.count() == LIMIT + 1

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
            "expire_soon_prescriptions": [],
            "prescriptions": [
                {
                    "id": prescriptions[1].id,
                    "patient": 1,
                    "prescribing_doctor": "Dr Leen",
                    "email_doctor": "dr.a@example.com",
                    "start_date": "2022-08-10",
                    "end_date": "2022-08-20",
                    "photo_prescription": None,
                    "is_valid": True,
                    "expiring_soon": False,
                },
                {
                    "id": prescriptions[0].id,
                    "patient": 1,
                    "prescribing_doctor": "Dr Leen",
                    "email_doctor": "dr.a@example.com",
                    "start_date": "2022-08-01",
                    "end_date": "2022-08-10",
                    "photo_prescription": None,
                    "is_valid": False,
                    "expiring_soon": False,
                },
            ],
        }

        assert response.json() == [expected_data]

    def test_patient_list_with_fields(self, user, client):
        patient = Patient.objects.create(**self.data)
        nurse, _ = Nurse.objects.get_or_create(user=user)
        patient.nurse_set.add(nurse)
        fields = "id,firstname,lastname,city,zip_code,street"
        response = client.get(self.url, {"fields": fields})
        assert response.status_code == status.HTTP_200_OK
        expected_data = {
            "id": 1,
            "firstname": "John",
            "lastname": "Leen",
            "street": "3 place du cerdan",
            "zip_code": "95400",
            "city": "courdimanche",
        }
        assert response.json() == [expected_data]

    def test_patient_list_401(self):
        """The endpoint should be under authentication."""
        response = APIClient().get(self.url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @freeze_time("2024-05-14")
    def test_patient_detail(self, user, client):
        patient = Patient.objects.create(**self.data)
        nurse, _ = Nurse.objects.get_or_create(user=user)
        patient.nurse_set.add(nurse)
        Prescription.objects.create(
            **{
                **prescription_data,
                **{
                    "patient": patient,
                    "start_date": "2024-05-14",
                    "end_date": "2024-05-16",
                },
            }
        )
        Prescription.objects.create(
            **{
                **prescription_data,
                **{
                    "patient": patient,
                    "start_date": "2022-08-01",
                    "end_date": "2022-08-10",
                },
            }
        )
        response = client.get(reverse_lazy("v1:patient-detail", kwargs={"pk": 1}))
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
            "expire_soon_prescriptions": [
                {
                    "end_date": "2024-05-16",
                    "id": 1,
                    "is_valid": True,
                    "expiring_soon": True,
                    "patient": 1,
                    "photo_prescription": None,
                    "prescribing_doctor": "Dr Leen",
                    "email_doctor": "dr.a@example.com",
                    "start_date": "2024-05-14",
                },
            ],
            "prescriptions": [
                {
                    "end_date": "2024-05-16",
                    "id": 1,
                    "is_valid": True,
                    "expiring_soon": True,
                    "patient": 1,
                    "photo_prescription": None,
                    "prescribing_doctor": "Dr Leen",
                    "email_doctor": "dr.a@example.com",
                    "start_date": "2024-05-14",
                },
                {
                    "end_date": "2022-08-10",
                    "id": 2,
                    "is_valid": False,
                    "expiring_soon": False,
                    "patient": 1,
                    "photo_prescription": None,
                    "prescribing_doctor": "Dr Leen",
                    "email_doctor": "dr.a@example.com",
                    "start_date": "2022-08-01",
                },
            ],
        }

    def test_patient_delete(self, user, client):
        patient = Patient.objects.create(**self.data)
        nurse, _ = Nurse.objects.get_or_create(user=user)
        patient.nurse_set.add(nurse)
        response = client.delete(reverse_lazy("v1:patient-detail", kwargs={"pk": 1}))
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert response.data is None
        assert Patient.objects.count() == 0


@pytest.mark.django_db
class TestPrescription:
    url = reverse_lazy("v1:prescription-list")
    data = prescription_data

    def test_endpoint(self):
        assert self.url == "/api/v1/prescription/"

    def test_create_prescription(self, client, user):
        patient = Patient.objects.create(**patient_data)
        Nurse.objects.get_or_create(user=user)[0].patients.add(patient)
        data = {**self.data, **{"patient": patient.id}}
        response = client.post(self.url, data, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert Prescription.objects.count() == 1
        prescription = Prescription.objects.get()
        assert prescription.prescribing_doctor == "Dr Leen"
        assert prescription.start_date == date(2022, 7, 15)
        assert prescription.end_date == date(2022, 7, 31)
        assert prescription.photo_prescription.name == ""
        assert prescription.patient.id == patient.id

    @override_settings(FREE_PRESCRIPTION_LIMIT=2)
    def test_non_subscribed_user_cannot_exceed_prescription_limit(self, client, user):
        """
        Ensure that a non-subscribed
        user cannot create more prescription than the allowed limit.
        """
        LIMIT = settings.FREE_PRESCRIPTION_LIMIT
        patient = Patient.objects.create(**patient_data)
        Nurse.objects.get_or_create(user=user)[0].patients.add(patient)
        data = {**self.data, **{"patient": patient.id}}
        for _ in range(LIMIT):
            response = client.post(self.url, data, format="json")
            assert response.status_code == status.HTTP_201_CREATED
        assert Prescription.objects.count() == LIMIT
        response = client.post(self.url, data, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.data == {"detail": FREE_LIMIT_MESSAGE}

    @override_settings(FREE_PRESCRIPTION_LIMIT=2)
    def test_subscribed_user_can_exceed_prescription_limit(self, user, client):
        """
        Ensure that a subscribed user
        can create more prescription than the allowed limit.
        """
        LIMIT = settings.FREE_PRESCRIPTION_LIMIT
        Subscription.objects.create(
            user=user, stripe_subscription_id="session_id_example", active=True
        )
        patient = Patient.objects.create(**patient_data)
        Nurse.objects.get_or_create(user=user)[0].patients.add(patient)
        data = {**self.data, **{"patient": patient.id}}
        for _ in range(LIMIT + 1):
            response = client.post(self.url, data, format="json")
            assert response.status_code == status.HTTP_201_CREATED
        assert Prescription.objects.count() == LIMIT + 1

    def test_create_prescription_no_patient(self, client):
        """A prescription should always be attached to a patient."""
        response = client.post(self.url, self.data, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data == {"patient": ["This field is required."]}

    def test_prescription_list(self, user, client):
        patient = Patient.objects.create(**patient_data)
        # creating a prescription with no nurse attached
        data = {**self.data, **{"patient": patient}}
        prescription = Prescription.objects.create(**data)
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
                "email_doctor": "dr.a@example.com",
                "start_date": "2022-07-15",
                "end_date": "2022-07-31",
                "photo_prescription": None,
                "patient": patient.id,
                "patient_firstname": "John",
                "patient_lastname": "Leen",
                "is_valid": False,
                "expiring_soon": False,
            }
        ]

    def test_prescription_list_401(self):
        """The endpoint should be under authentication."""
        response = APIClient().get(self.url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @freeze_time("2022-07-20")
    def test_prescription_detail(self, user, client):
        patient = Patient.objects.create(**patient_data)
        data = {**self.data, **{"patient": patient}}
        prescription = Prescription.objects.create(**data)
        response = client.get(reverse_lazy("v1:prescription-detail", kwargs={"pk": 1}))
        # the prescription/patient is not linked to the logged nurse
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data == {"detail": "No Prescription matches the given query."}
        # let's link patient, nurse and prescription together
        patient, _, _ = attach_prescription(prescription, user)
        response = client.get(reverse_lazy("v1:prescription-detail", kwargs={"pk": 1}))
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "id": 1,
            "prescribing_doctor": "Dr Leen",
            "email_doctor": "dr.a@example.com",
            "start_date": "2022-07-15",
            "end_date": "2022-07-31",
            "photo_prescription": None,
            "patient": patient.id,
            "patient_firstname": "John",
            "patient_lastname": "Leen",
            "is_valid": True,
            "expiring_soon": False,
        }

    def test_prescription_delete(self, user, client):
        patient = Patient.objects.create(**patient_data)
        data = {**self.data, **{"patient": patient}}
        prescription = Prescription.objects.create(**data)
        response = client.delete(
            reverse_lazy("v1:prescription-detail", kwargs={"pk": prescription.id})
        )
        # the prescription/patient is not linked to the logged nurse
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data == {"detail": "No Prescription matches the given query."}
        # let's link patient, nurse and prescription together
        patient, _, _ = attach_prescription(prescription, user)
        response = client.delete(
            reverse_lazy("v1:prescription-detail", kwargs={"pk": prescription.id})
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert response.data is None
        assert Prescription.objects.count() == 0

    # Access checks are covered in test_prescription_access.py.
    def test_prescription_upload(self, s3_mock, client, user):
        patient = Patient.objects.create(**patient_data)
        Nurse.objects.get_or_create(user=user)[0].patients.add(patient)
        data = {**self.data, **{"patient": patient}}
        prescription = Prescription.objects.create(**data)
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
            reverse_lazy("v1:prescription-upload", kwargs={"pk": prescription.id}), data
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
    url = reverse_lazy("v1:nurse-list")
    data = {
        "user": 1,
        "phone": "0134643232",
        "address": "3 rue de pontoise",
        "zip_code": "95300",
        "city": "Pontoise",
    }

    def test_endpoint(self):
        assert self.url == "/api/v1/nurse/"

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
        response = client.get(reverse_lazy("v1:nurse-detail", kwargs={"pk": 1}))
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
    url = reverse_lazy("v1:user-list")
    data = {
        "username": "@Issa",
        "email": "issa_test@test.com",
        "password": "password123!@",
    }

    def test_endpoint(self):
        assert self.url == "/api/v1/user/"

    def test_create_user(self, client):
        """The user creation is via a different endpoint (/account/register)."""
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
                "first_name": "John",
                "last_name": "Doe",
                "email": EMAIL,
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
        assert response.data == expected_response

    def test_detail_user(self, user, client):
        response = client.get(reverse_lazy("v1:user-detail", kwargs={"pk": None}))
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "id": 1,
            "username": user.username,
            "first_name": "John",
            "last_name": "Doe",
            "email": EMAIL,
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
            reverse_lazy("v1:user-detail", kwargs={"pk": 1}),
            self.data,
            format="json",
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert (
            response.data["detail"]
            == "You do not have permission to perform this action."
        )

    def test_update_user(self, user, client):
        """Update first and last names."""
        assert user.first_name == "John"
        assert user.last_name == "Doe"
        assert user.email == "johndoe@example.fr"
        data = {**self.data, "first_name": "Firstname1", "last_name": "Lastname 1"}
        assert user.email != data["email"]
        response = client.put(
            reverse_lazy("v1:user-detail", kwargs={"pk": None}), data, format="json"
        )
        assert response.status_code == status.HTTP_200_OK
        data.pop("password")
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
                "user": user.id,
                "zip_code": "",
            },
        }
        user.refresh_from_db()
        assert user.first_name == data["first_name"]
        assert user.last_name == data["last_name"]
        assert user.email == data["email"]

    def test_partial_update_user(self, user, client):
        """Using a patch for a partial update (not all fields)."""
        assert user.first_name == "John"
        assert user.last_name == "Doe"
        assert user.email == "johndoe@example.fr"
        data = {"first_name": "Firstname1", "last_name": "Lastname 1"}
        response = client.patch(
            reverse_lazy("v1:user-detail", kwargs={"pk": None}), data, format="json"
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "id": 1,
            "username": user.username,
            "email": EMAIL,
            "is_staff": False,
            **data,
            "nurse": {
                "id": 1,
                "address": "",
                "city": "",
                "patients": [],
                "phone": "",
                "user": user.id,
                "zip_code": "",
            },
        }
        user.refresh_from_db()
        assert user.first_name == data["first_name"]
        assert user.last_name == data["last_name"]
        # email wasn't in the payload but didn't get voided since it's a PATCH call
        assert user.email == "johndoe@example.fr"

    def test_delete_user(self, client):
        response = client.delete(reverse_lazy("v1:user-detail", kwargs={"pk": None}))
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert response.data is None
        assert User.objects.count() == 0


@pytest.mark.django_db
class TestUserCreate:
    client = APIClient()
    url = reverse_lazy("v1:register")
    username = {"username": USERNAME}
    password = {"password": PASSWORD}
    email = {"email": EMAIL}
    data = {**username, **password, **email}

    def test_url(self):
        assert self.url == "/api/v1/account/register"

    def test_create(self):
        """Create a User using username and password."""
        assert User.objects.filter(**self.username).count() == 0
        response = self.client.post(self.url, self.data, format="json")
        assert response.json() == {
            "id": 1,
            "username": USERNAME,
            "first_name": "",
            "last_name": "",
            "email": EMAIL,
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
        assert user.username == USERNAME
        assert user.email == EMAIL

    def test_create_already_exists(self):
        assert User.objects.filter(**self.username).count() == 0
        response = self.client.post(self.url, self.data, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert User.objects.filter(**self.username).count() == 1
        response = self.client.post(self.url, self.data, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data == {
            "username": ["A user with that username already exists."]
        }
        assert User.objects.filter(**self.username).count() == 1

    def test_get(self):
        """It's not allowed to list all users."""
        response = self.client.get(self.url)
        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
        assert response.data == {"detail": 'Method "GET" not allowed.'}

    def test_auth(self):
        """Note this isn't a REST endpoint."""
        url = reverse_lazy("rest_framework:login")
        assert url == "/account/login/"
        user = User.objects.create(**self.username)
        user.set_password(self.password["password"])
        user.save()
        data = {**self.username, **self.password}
        response = self.client.post(url, data)
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
class TestUserCreateV2:
    client = APIClient()
    url = reverse_lazy("v2:register")
    username = {"username": USERNAME}
    password = {"password": PASSWORD}
    email = {"email": EMAIL}
    data = {**username, **password, **email}

    def test_url(self):
        assert self.url == "/api/v2/account/register"

    def test_create(self):
        """Create a User using email and password, username should be overridden."""
        data = {**self.password, **self.email}
        assert User.objects.filter(**self.email).count() == 0
        response = self.client.post(self.url, data, format="json")
        assert response.json() == {
            "id": 1,
            "first_name": "",
            "last_name": "",
            "email": EMAIL,
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
        users = User.objects.filter(**self.email)
        assert users.count() == 1
        user = users.get()
        assert user.check_password(self.password["password"]) is True
        assert user.nurse is not None
        # the username passed in the payload was ignored and the email was used instead
        assert user.username == EMAIL
        assert user.email == EMAIL


@pytest.mark.django_db
class TestProfile:
    url = "/api/v1/profile/"

    def test_endpoint(self):
        assert self.url == reverse_lazy("v1:profile")

    def test_get(self, client):
        response = client.get(self.url)
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "id": 1,
            "username": USERNAME,
            "first_name": "John",
            "last_name": "Doe",
            "email": EMAIL,
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

    def test_get_401(self):
        """The endpoint should be under authentication."""
        response = APIClient().get(self.url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
class TestAdminNotificationView:
    url = reverse_lazy("v1:notify")

    def test_endpoint_patient(self):
        assert self.url == "/api/v1/notify/"

    def test_post(self, staff_client):
        """Posting to the endpoint should send notifications."""
        with patch_notify() as mock_notify:
            response = staff_client.post(self.url, {}, format="json")
        assert mock_notify.call_args_list == [mock.call()]
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert response.data is None

    def test_post_unauthenticated(self):
        """Unauthenticated clients aren't allowed."""
        with patch_notify() as mock_notify:
            response = APIClient().post(self.url, {}, format="json")
        assert mock_notify.call_args_list == []
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json() == {
            "detail": "Authentication credentials were not provided."
        }

    def test_post_only_staff(self, client):
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
    url = reverse_lazy("v1:useronesignalprofile-list")

    def test_endpoint_onesignal(self):
        assert self.url == "/api/v1/onesignal/"

    def test_create_onesignal(self, client, user):
        subscription_id = "1233456789"
        data = {"subscription_id": subscription_id}
        client.force_authenticate(user=user)
        assert UserOneSignalProfile.objects.count() == 0
        response = client.post(self.url, data, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data == {"user": user.id, "subscription_id": subscription_id}
        assert UserOneSignalProfile.objects.count() == 1
        profile = UserOneSignalProfile.objects.first()
        assert profile.subscription_id == subscription_id

    def test_read_onesignal_list(
        self, client, user, one_signal_profile, one_signal_profile2
    ):
        client.force_authenticate(user=user)
        response = client.get(self.url)
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data == [
            {"subscription_id": one_signal_profile.subscription_id, "user": user.id}
        ]

    def test_onesignal_list_401(self):
        """The endpoint should be under authentication."""
        response = APIClient().get(self.url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
