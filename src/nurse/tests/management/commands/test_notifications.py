from datetime import datetime, timedelta
from unittest import mock

import pytest
from django.test import override_settings

from nurse.management.commands import _notifications
from nurse.models import (
    Nurse,
    Patient,
    Prescription,
    PrescriptionManager,
    User,
    UserOneSignalProfile,
)

ONESIGNAL_APP_ID = "test_app_id"
ONESIGNAL_API_KEY = "test_api_key"
client_path = "nurse.management.commands._notifications.Client"


@pytest.fixture
def base_prescriptions():
    user = User.objects.create(username="nurse1")
    nurse = Nurse.objects.create(user=user)
    patient = Patient.objects.create(firstname="Patient 1")
    patient.nurse_set.add(nurse)
    Prescription.objects.create(
        prescribing_doctor="Dr A",
        patient=patient,
        start_date=datetime.now().date() - timedelta(days=3),
        end_date=datetime.now().date()
        + timedelta(days=PrescriptionManager.DEFAULT_EXPIRING_SOON_DAYS - 2),
    )
    UserOneSignalProfile.objects.create(user=user, subscription_id="123")

    # Create second nurse same patient
    user = User.objects.create(username="nurse11")
    nurse = Nurse.objects.create(user=user)
    patient.nurse_set.add(nurse)
    Prescription.objects.create(
        prescribing_doctor="Dr A",
        patient=patient,
        start_date=datetime.now().date() - timedelta(days=3),
        end_date=datetime.now().date()
        + timedelta(days=PrescriptionManager.DEFAULT_EXPIRING_SOON_DAYS - 2),
    )
    UserOneSignalProfile.objects.create(user=user, subscription_id="456")

    user = User.objects.create(username="nurse2")
    nurse = Nurse.objects.create(user=user)
    patient = Patient.objects.create(firstname="Patient 2")
    patient.nurse_set.add(nurse)
    Prescription.objects.create(
        prescribing_doctor="Dr B",
        patient=patient,
        start_date=datetime.now().date() - timedelta(days=1),
        end_date=datetime.now().date()
        + timedelta(days=PrescriptionManager.DEFAULT_EXPIRING_SOON_DAYS - 1),
    )
    UserOneSignalProfile.objects.create(user=user, subscription_id="789")

    user = User.objects.create(username="nurse3")
    nurse = Nurse.objects.create(user=user)
    patient = Patient.objects.create(firstname="Patient 3")
    patient.nurse_set.add(nurse)
    Prescription.objects.create(
        prescribing_doctor="Dr C",
        patient=patient,
        start_date=datetime.now().date() - timedelta(days=1),
        end_date=datetime.now().date()
        + timedelta(days=PrescriptionManager.DEFAULT_EXPIRING_SOON_DAYS + 1),
    )
    UserOneSignalProfile.objects.create(user=user, subscription_id="abc")


class TestNotifications:
    @pytest.mark.django_db
    def test_notify(self, base_prescriptions):
        with mock.patch(
            f"{client_path}.send_notification"
        ) as mock_send_notification, override_settings(
            ONESIGNAL_APP_ID="ONESIGNAL_APP_ID", ONESIGNAL_API_KEY="ONESIGNAL_API_KEY"
        ):
            _notifications.notify()
        expected_include_subscription_ids = {"123", "456", "789"}
        expected_notification_body = {
            "contents": {
                "fr": "Une ordonnance est sur le point d'expirer, "
                "ouvrez l'application pour la consulter.",
                "en": "A prescription is about to expire, open the app to review.",
            },
            "include_subscription_ids": expected_include_subscription_ids,
            "name": "PRESCRIPTION EXPIRE SOON",
        }
        assert mock_send_notification.call_args_list == [
            mock.call(expected_notification_body)
        ]

    def test_get_client_with_valid_settings(self):
        with override_settings(
            ONESIGNAL_APP_ID=ONESIGNAL_APP_ID,
            ONESIGNAL_API_KEY=ONESIGNAL_API_KEY,
        ), mock.patch(client_path) as mock_client:
            client = _notifications.get_client()

            mock_client.assert_called_once_with(
                app_id=ONESIGNAL_APP_ID, rest_api_key=ONESIGNAL_API_KEY
            )
            assert client == mock_client.return_value

    @pytest.mark.parametrize(
        "app_id, api_key, expected_error",
        [
            (None, ONESIGNAL_API_KEY, "ONESIGNAL_APP_ID must be set"),
            (ONESIGNAL_APP_ID, None, "ONESIGNAL_API_KEY must be set"),
        ],
    )
    def test_get_client_with_missing_settings(self, app_id, api_key, expected_error):
        with override_settings(
            ONESIGNAL_APP_ID=app_id,
            ONESIGNAL_API_KEY=api_key,
        ), pytest.raises(AssertionError, match=expected_error):
            _notifications.get_client()
