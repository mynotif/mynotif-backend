from django.urls.base import reverse_lazy
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Patient


class PatientTests(APITestCase):
    url = reverse_lazy("patient-list")

    data = {
        "firstname": "John",
        "lastname": "Leen",
        "address": "3 place du cerdan",
        "zip_code": "95400",
        "city": "courdimanche",
        "phone": "0602015454",
    }

    def test_create_patient(self):
        response = self.client.post(self.url, self.data, format="json")
        assert response.status_code == status.HTTP_201_CREATED
        assert Patient.objects.count() == 1
        patient = Patient.objects.get(phone="0602015454")
        assert patient.firstname == "John"
        assert patient.lastname == "Leen"
        assert patient.address == "3 place du cerdan"
        assert patient.zip_code == "95400"
        assert patient.city == "courdimanche"

    def test_patient_list(self):
        Patient.objects.create(**self.data)
        response = self.client.get(self.url)
        assert response.status_code == status.HTTP_200_OK
        assert response.data == [
            {
                "id": 1,
                "firstname": "John",
                "lastname": "Leen",
                "address": "3 place du cerdan",
                "zip_code": "95400",
                "city": "courdimanche",
                "phone": "0602015454",
            }
        ]

    def test_patient_detail(self):
        Patient.objects.create(**self.data)
        response = self.client.get(reverse_lazy("patient-detail", kwargs={"pk": 1}))
        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            "id": 1,
            "firstname": "John",
            "lastname": "Leen",
            "address": "3 place du cerdan",
            "zip_code": "95400",
            "city": "courdimanche",
            "phone": "0602015454",
        }

    def test_patient_delete(self):
        Patient.objects.create(**self.data)
        response = self.client.delete(reverse_lazy("patient-detail", kwargs={"pk": 1}))
        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert response.data is None


class PrescriptionTests(APITestCase):

    url = reverse_lazy("prescription-list")

    data = {
        "carte_vitale": "12345678910",
        "caisse_rattachement": "12345678910",
        "prescribing_doctor": "Dr Leen",
        "start_date": "2022-07-15",
        "end_date": "2022-07-31",
        "at_renew": 1,
        "photo_prescription": "path_image",
    }

    def test_create_prescription(self):
        response = self.client.post(self.url, self.data, format="json")
        # FIXME: this needs to be addressed to handle POST properly
        assert response.status_code == status.HTTP_400_BAD_REQUEST
