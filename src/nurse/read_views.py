from drf_spectacular.utils import OpenApiParameter, extend_schema

from nurse.models import Patient
from nurse.read_api import ERROR_RESPONSES, PAGE_PARAMETERS, PrivateReadView
from nurse.read_serializers import (
    PatientPageSerializer,
    PatientQuerySerializer,
    PatientSummarySerializer,
)


class PatientReadList(PrivateReadView):
    query_serializer_class = PatientQuerySerializer

    @extend_schema(
        operation_id="v2_patients_list",
        description=(
            "Lecture Bearer M02, isolée par Nurse, sans création de profil. "
            "Tri id ASC, pagination serveur après recherche globale. "
            "HEAD sans corps ; OPTIONS authentifié sans données métier. "
            "Cache-Control: private, no-store ; Pragma: no-cache. "
            "Paramètres inconnus ou dupliqués refusés."
        ),
        parameters=PAGE_PARAMETERS
        + [
            OpenApiParameter(
                "search",
                {"type": "string", "maxLength": 100},
                description=(
                    "Unique, ≤100 points de code avant NFD/retrait Unicode M/"
                    "casefold/split espaces Unicode ; AND termes, OR "
                    "firstname/lastname/city/phone. %, _, * littéraux."
                ),
            )
        ],
        responses={200: PatientPageSerializer, **ERROR_RESPONSES},
    )
    def get(self, request):
        query = self.query(request)
        patients = (
            Patient.objects.filter(nurse__user=request.user)
            .search_normalized(query["search"])
            .distinct()
            .order_by("id")
            .only(*PatientSummarySerializer.Meta.fields)
        )
        return self.page_response(patients, query, PatientSummarySerializer)
