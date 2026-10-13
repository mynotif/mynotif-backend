import re

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response

from nurse.models import Patient, Prescription
from nurse.read_api import (
    ERROR_RESPONSES,
    PAGE_PARAMETERS,
    PrivateReadView,
    ReadAPIError,
)
from nurse.read_prescription_serializers import (
    PrescriptionDetailQuerySerializer,
    PrescriptionPageSerializer,
    PrescriptionSummarySerializer,
)


def positive_id(value):
    if not re.fullmatch(r"[0-9]+", value, flags=re.ASCII):
        raise ReadAPIError("not_found")
    digits = value.lstrip("0")
    if not digits or len(digits) > 19 or int(digits) > 9223372036854775807:
        raise ReadAPIError("not_found")
    return int(digits)


PATIENT_ID = OpenApiParameter(
    "patient_id",
    {"type": "integer", "minimum": 1, "maximum": 9223372036854775807},
    OpenApiParameter.PATH,
    description="Patient associé au Nurse courant.",
)
PRESCRIPTION_ID = OpenApiParameter(
    "prescription_id",
    {"type": "integer", "minimum": 1, "maximum": 9223372036854775807},
    OpenApiParameter.PATH,
    description="Ordonnance appartenant au patient autorisé.",
)


class PatientPrescriptionRead(PrivateReadView):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        patient_id = positive_id(kwargs["patient_id"])
        self.patient = get_object_or_404(
            Patient.objects.filter(nurse__user=request.user).only("id"), pk=patient_id
        )
        if "prescription_id" in kwargs:
            prescription_id = positive_id(kwargs["prescription_id"])
            self.prescription = get_object_or_404(
                Prescription.objects.filter(
                    patient_id=self.patient.pk,
                    document_versions__isnull=True,
                ).only(
                    *PrescriptionSummarySerializer.Meta.fields
                ),
                pk=prescription_id,
            )


class PrescriptionReadList(PatientPrescriptionRead):
    @extend_schema(
        operation_id="v2_patient_prescriptions_list",
        description=(
            "Résumés sans document/URL/email/statut calculé ni accès stockage. "
            "Bearer M02 puis autorisation Nurse → patient avant query. "
            "Tri end_date DESC, id DESC ; pagination serveur 20/100. "
            "HEAD sans corps ; OPTIONS même autorisation sans métier. "
            "Succès et erreurs private, no-store et Pragma no-cache."
        ),
        parameters=[PATIENT_ID, *PAGE_PARAMETERS],
        responses={200: PrescriptionPageSerializer, **ERROR_RESPONSES},
    )
    def get(self, request, patient_id):
        query = self.query(request)
        prescriptions = (
            Prescription.objects.filter(
                patient_id=self.patient.pk,
                document_versions__isnull=True,
            )
            .order_by("-end_date", "-id")
            .only(*PrescriptionSummarySerializer.Meta.fields)
        )
        return self.page_response(prescriptions, query, PrescriptionSummarySerializer)


class PrescriptionReadDetail(PatientPrescriptionRead):
    query_serializer_class = PrescriptionDetailQuerySerializer

    @extend_schema(
        operation_id="v2_patient_prescriptions_retrieve",
        description=(
            "Même résumé minimal que la liste, dates source inchangées. "
            "Bearer M02 puis Nurse → patient → ordonnance avant query. "
            "Aucun paramètre query accepté. HEAD sans corps ; OPTIONS autorisé "
            "sans données métier. Aucun appel stockage. private, no-store."
        ),
        parameters=[PATIENT_ID, PRESCRIPTION_ID],
        responses={200: PrescriptionSummarySerializer, **ERROR_RESPONSES},
    )
    def get(self, request, patient_id, prescription_id):
        self.query(request)
        return Response(PrescriptionSummarySerializer(self.prescription).data)
