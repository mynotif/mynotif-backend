"""Private, Bearer-only read infrastructure, scoped away from v1 and M02."""

import math

from django.core.exceptions import BadRequest, SuspiciousOperation
from django.db import InterfaceError, OperationalError
from django.http import Http404
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import OpenApiParameter
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    MethodNotAllowed,
    NotAuthenticated,
    NotFound,
    Throttled,
    ValidationError,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from main.session_authentication import OpaqueSessionAuthentication
from nurse.read_serializers import ReadErrorSerializer, ReadQuerySerializer

MESSAGES = {
    "invalid_query": (400, "Paramètres de lecture invalides."),
    "authentication_required": (401, "Authentification requise."),
    "not_found": (404, "Ressource indisponible."),
    "page_not_found": (404, "Page indisponible."),
    "method_not_allowed": (405, "Méthode non autorisée."),
    "rate_limited": (429, "Trop de demandes. Réessayez plus tard."),
    "service_unavailable": (503, "Service indisponible. Réessayez."),
}
ERROR_RESPONSES = {code: ReadErrorSerializer for code in (400, 401, 404, 405, 429, 503)}
PAGE_PARAMETERS = [
    OpenApiParameter(
        "page",
        {"type": "integer", "minimum": 1, "maximum": 2147483647},
        description="Unique, [1-9][0-9]* ; défaut 1.",
        default=1,
        enum=None,
    ),
    OpenApiParameter(
        "page_size",
        {"type": "integer", "minimum": 1, "maximum": 100},
        description="Unique, [1-9][0-9]* ; 1…100, défaut 20.",
        default=20,
    ),
]


class ReadAPIError(APIException):
    def __init__(self, code):
        self.status_code, message = MESSAGES[code]
        self.detail = {"code": code, "message": message}


class ClosedReadSchema(AutoSchema):
    def _map_serializer(self, serializer, direction, bypass_extensions=False):
        schema = super()._map_serializer(serializer, direction, bypass_extensions)
        if serializer.__class__.__module__.startswith("nurse.read_"):
            schema["additionalProperties"] = False
            if direction == "response":
                schema["required"] = sorted(schema.get("properties", {}))
        return schema


class PrivateReadView(APIView):
    schema = ClosedReadSchema()
    authentication_classes = [OpaqueSessionAuthentication]
    permission_classes = [IsAuthenticated]
    renderer_classes = [JSONRenderer]
    http_method_names = ["get", "head", "options"]
    throttle_classes = []
    query_serializer_class = ReadQuerySerializer

    def perform_content_negotiation(self, request, force=False):
        # The closed read contract always returns JSON, including failures.
        renderer = self.get_renderers()[0]
        return renderer, renderer.media_type

    def handle_exception(self, exc):
        if isinstance(exc, (AuthenticationFailed, NotAuthenticated)):
            error = ReadAPIError("authentication_required")
        elif isinstance(exc, ReadAPIError):
            error = exc
        elif isinstance(exc, (ValidationError, BadRequest, SuspiciousOperation)):
            error = ReadAPIError("invalid_query")
        elif isinstance(exc, (NotFound, Http404)):
            error = ReadAPIError("not_found")
        elif isinstance(exc, MethodNotAllowed):
            error = ReadAPIError("method_not_allowed")
        elif isinstance(exc, Throttled):
            error = ReadAPIError("rate_limited")
        elif isinstance(exc, (OperationalError, InterfaceError)):
            error = ReadAPIError("service_unavailable")
        else:
            return super().handle_exception(exc)
        response = Response(error.detail, status=error.status_code)
        if error.status_code == 401:
            response["WWW-Authenticate"] = "Bearer"
        if isinstance(exc, Throttled):
            response["Retry-After"] = str(max(0, math.ceil(exc.wait or 0)))
        return response

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        response.headers.pop("ETag", None)
        return response

    def query(self, request):
        serializer = self.query_serializer_class(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def page_response(self, queryset, query, serializer_class):
        count = queryset.count()
        page, page_size = query["page"], query["page_size"]
        last_page = max(1, (count + page_size - 1) // page_size)
        if page > last_page:
            raise ReadAPIError("page_not_found")
        offset = (page - 1) * page_size
        return Response(
            {
                "count": count,
                "page": page,
                "page_size": page_size,
                "next_page": page + 1 if page < last_page else None,
                "previous_page": page - 1 if page > 1 else None,
                "results": serializer_class(
                    queryset[offset : offset + page_size], many=True
                ).data,
            }
        )

    def options(self, request, *args, **kwargs):
        self.query(request)
        return Response({"allowed_methods": self.allowed_methods})
