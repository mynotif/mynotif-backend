from django.urls import path

from nurse.document_read_views import (
    PrescriptionDetail,
    PrescriptionDocumentContent,
    PrescriptionDocumentManifest,
    PrescriptionList,
)

urlpatterns = [
    path(
        "patients/<int:patient_id>/prescriptions/",
        PrescriptionList.as_view(),
        name="prescriptions",
    ),
    path(
        "patients/<int:patient_id>/prescriptions/<int:prescription_id>/",
        PrescriptionDetail.as_view(),
        name="prescription",
    ),
    path(
        "patients/<int:patient_id>/prescriptions/<int:prescription_id>/documents/",
        PrescriptionDocumentManifest.as_view(),
        name="prescription-document-manifest",
    ),
    path(
        "patients/<int:patient_id>/prescriptions/<int:prescription_id>/documents/"
        "<uuid:asset_id>/content/",
        PrescriptionDocumentContent.as_view(),
        name="prescription-document-content",
    ),
]
