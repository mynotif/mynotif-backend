from django.urls import path, re_path

from nurse.read_prescription_views import PrescriptionReadDetail, PrescriptionReadList
from nurse.read_views import PatientReadList

urlpatterns = [
    path("patients/", PatientReadList.as_view(), name="patients-list"),
    re_path(
        r"^patients/(?P<patient_id>[^/]*)/prescriptions/$",
        PrescriptionReadList.as_view(),
        name="prescriptions-list",
    ),
    re_path(
        r"^patients/(?P<patient_id>[^/]*)/prescriptions/(?P<prescription_id>[^/]*)/$",
        PrescriptionReadDetail.as_view(),
        name="prescriptions-detail",
    ),
]
