from django.urls import path

from nurse.expiry_views import ExpiryPrescriptionDetail, ExpiryPrescriptionList

urlpatterns = [
    path(
        "prescriptions/", ExpiryPrescriptionList.as_view(), name="expiry-prescriptions"
    ),
    path(
        "patients/<str:patient_id>/prescriptions/<str:prescription_id>/",
        ExpiryPrescriptionDetail.as_view(),
        name="expiry-prescription-detail",
    ),
]
