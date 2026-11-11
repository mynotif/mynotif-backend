"""Private correction endpoints. GET observes; only explicit POST/PUT write."""

from drf_spectacular.utils import (
    OpenApiParameter,
    PolymorphicProxySerializer,
    extend_schema,
)
from rest_framework.response import Response

from nurse.expiry_command_runtime import runtime
from nurse.expiry_command_serializers import (
    ExpiryAdmissionSerializer,
    ExpiryCommandPageSerializer,
    ExpiryCommandSerializer,
    ExpiryCorrectionAdmissionSerializer,
    ExpiryCorrectionSerializer,
    ExpiryFenceRequestSerializer,
    ExpiryFenceSerializer,
    ExpiryRenewalAdmissionSerializer,
    ExpiryRenewalSerializer,
    StrictJSONParser,
)
from nurse.expiry_commands import (
    accessible_commands,
    admit_command,
    command_data,
    execute_command,
    fence,
    journal_revision,
    lookup_command,
)
from nurse.expiry_state import ExpiryError, cursor_offset, next_cursor
from nurse.expiry_views import ERROR_RESPONSES, ExpiryReadView

COMMAND_PARAMETER = [
    OpenApiParameter(
        "command_id", {"type": "string", "format": "uuid"}, OpenApiParameter.PATH
    )
]
CURSOR_PARAMETER = [OpenApiParameter("cursor", {"type": "string", "maxLength": 2048})]


class ExpiryCommandView(ExpiryReadView):
    parser_classes = [StrictJSONParser]

    def query(self, request):
        query = request.query_params
        allowed = (
            {"cursor"}
            if request.method in ("GET", "HEAD") and not self.kwargs
            else set()
        )
        if any(key not in allowed or len(query.getlist(key)) != 1 for key in query):
            raise ExpiryError("invalid_request")
        raw = query.get("cursor")
        if raw is not None and (not raw or len(raw) > 2048):
            raise ExpiryError("invalid_cursor")
        return raw

    def body(self, request, serializer_class):
        self.query(request)
        serializer = serializer_class(data=request.data)
        if not serializer.is_valid():
            raise ExpiryError("invalid_request")
        return serializer.validated_data

    def options(self, request, **kwargs):
        self.query(request)
        state, context = self.begin(request)
        if "command_id" in kwargs:
            row = lookup_command(request.user.pk, state[0], kwargs["command_id"])
            if (
                not accessible_commands(request.user.pk, state[0])
                .filter(pk=row.pk)
                .exists()
            ):
                raise ExpiryError("not_found")
        self.finish(request, state, context)
        return Response({"allowed_methods": ", ".join(self.allowed_methods)})


class ExpiryCommands(ExpiryCommandView):
    http_method_names = ["get", "head", "post", "options"]

    @extend_schema(
        operation_id="v4_expiry_commands_list",
        parameters=CURSOR_PARAMETER,
        responses={200: ExpiryCommandPageSerializer, **ERROR_RESPONSES},
    )
    def get(self, request):
        raw = self.query(request)
        state, context = self.begin(request)
        revision = journal_revision(request.user.pk, state)
        bound = {
            "account": request.user.pk,
            "scope": list(state[:3]),
            "journal": revision,
        }
        offset = cursor_offset(raw, bound)
        rows = list(
            accessible_commands(request.user.pk, state[0]).order_by(
                "-recorded_at", "command_id"
            )[offset : offset + 21]
        )
        following = next_cursor(bound, offset + 20) if len(rows) > 20 else None
        self.finish(request, state, context)
        if journal_revision(request.user.pk, state) != revision:
            raise ExpiryError("pagination_invalidated")
        return Response(
            {
                "contract_revision": "0.2-decision",
                "mutation_epoch": state[2],
                "journal_revision": revision,
                "next_cursor": following,
                "results": [command_data(row) for row in rows[:20]],
            }
        )

    @extend_schema(
        operation_id="v4_expiry_commands_create",
        request=PolymorphicProxySerializer(
            component_name="ExpiryAdmission",
            serializers=[
                ExpiryCorrectionAdmissionSerializer,
                ExpiryRenewalAdmissionSerializer,
            ],
            resource_type_field_name=None,
        ),
        responses={201: ExpiryCommandSerializer, **ERROR_RESPONSES},
    )
    def post(self, request):
        body = self.body(request, ExpiryAdmissionSerializer)
        return Response(
            admit_command(request, body["intent"], body["kind"]), status=201
        )


class ExpiryCommandDetail(ExpiryCommandView):
    http_method_names = ["get", "head", "options"]

    @extend_schema(
        operation_id="v4_expiry_commands_retrieve",
        parameters=COMMAND_PARAMETER,
        responses={200: ExpiryCommandSerializer, **ERROR_RESPONSES},
    )
    def get(self, request, command_id):
        self.query(request)
        state, context = self.begin(request)
        row = lookup_command(request.user.pk, state[0], command_id)
        if (
            not accessible_commands(request.user.pk, state[0])
            .filter(pk=row.pk)
            .exists()
        ):
            raise ExpiryError("not_found")
        self.finish(request, state, context)
        return Response(command_data(row))


class ExpiryDateCorrection(ExpiryCommandView):
    http_method_names = ["put", "options"]

    @extend_schema(
        operation_id="v4_expiry_date_correction",
        parameters=COMMAND_PARAMETER,
        request=ExpiryCorrectionSerializer,
        responses={
            200: ExpiryCommandSerializer,
            202: ExpiryCommandSerializer,
            **ERROR_RESPONSES,
        },
    )
    def put(self, request, command_id):
        intent = self.body(request, ExpiryCorrectionSerializer)
        return Response(execute_command(request, command_id, intent))


class ExpiryRenewalConfirmation(ExpiryCommandView):
    http_method_names = ["put", "options"]

    @extend_schema(
        operation_id="v4_expiry_renewal_confirmation",
        parameters=COMMAND_PARAMETER,
        request=ExpiryRenewalSerializer,
        responses={
            200: ExpiryCommandSerializer,
            202: ExpiryCommandSerializer,
            **ERROR_RESPONSES,
        },
    )
    def put(self, request, command_id):
        runtime("renewal_confirmation")
        intent = self.body(request, ExpiryRenewalSerializer)
        return Response(
            execute_command(request, command_id, intent, "renewal_confirmation")
        )


class ExpiryReconciliationFence(ExpiryCommandView):
    http_method_names = ["post", "options"]

    @extend_schema(
        operation_id="v4_expiry_reconciliation_fence",
        request=ExpiryFenceRequestSerializer,
        responses={200: ExpiryFenceSerializer, **ERROR_RESPONSES},
    )
    def post(self, request):
        self.body(request, ExpiryFenceRequestSerializer)
        return Response(fence(request))
