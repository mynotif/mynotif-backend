from django.urls import path

from nurse.document_dispatch import read_view

urlpatterns = [
    path(
        "patients/<int:patient_id>/prescriptions/",
        read_view("prescriptions"),
        name="prescriptions",
    ),
    path(
        "patients/<int:patient_id>/prescriptions/<int:prescription_id>/",
        read_view("prescription"),
        name="prescription",
    ),
    path(
        "patients/<int:patient_id>/prescriptions/<int:prescription_id>/documents/",
        read_view("document-manifest"),
        name="prescription-document-manifest",
    ),
    path(
        "patients/<int:patient_id>/prescriptions/<int:prescription_id>/documents/"
        "<uuid:asset_id>/content/",
        read_view("prescription-document-content"),
        name="prescription-document-content",
    ),
]
