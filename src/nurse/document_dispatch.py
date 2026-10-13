"""Versioned document API dispatch with Bearer-only authentication."""

from importlib import import_module
import logging

from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from main.session_authentication import OpaqueSessionAuthentication

logger = logging.getLogger(__name__)

_ERRORS = {
    400: ("invalid_metadata", "Les informations du document sont invalides."),
    401: ("authentication_required", "Authentification requise."),
    404: ("not_found", "Ressource indisponible."),
    405: ("method_not_allowed", "Méthode non autorisée."),
    429: ("rate_limited", "Trop de demandes. Réessayez plus tard."),
    503: ("service_unavailable", "Service indisponible. Réessayez."),
}

IMPORT_HANDLER = "nurse.document_import_views"
READ_HANDLER = "nurse.document_read_views"


class DocumentV3DispatchView(APIView):
    authentication_classes = [OpaqueSessionAuthentication]
    permission_classes = [IsAuthenticated]
    renderer_classes = [JSONRenderer]
    throttle_classes = []
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]
    handler_module = None
    operation = None

    def handle_exception(self, exc):
        try:
            response = super().handle_exception(exc)
        except Exception:
            logger.exception("Private v3 request failed")
            response = Response(status=503)

        status_code = response.status_code
        if isinstance(exc, (AuthenticationFailed, NotAuthenticated)):
            status_code = 401
        code, message = _ERRORS.get(
            status_code, _ERRORS[503] if status_code >= 500 else _ERRORS[400]
        )
        response.data = {"code": code, "message": message, "fields": {}}
        response.status_code = status_code
        if status_code == 401:
            response["WWW-Authenticate"] = "Bearer"
        return response

    def dispatch_operation(self, request, *args, **kwargs):
        try:
            handler = import_module(self.handler_module)
        except ModuleNotFoundError as error:
            if error.name != self.handler_module:
                raise
            return Response(
                {
                    "code": "service_unavailable",
                    "message": "Service indisponible. Réessayez.",
                    "fields": {},
                },
                status=503,
            )
        return handler.dispatch(self.operation, request, **kwargs)

    def get(self, request, *args, **kwargs):
        return self.dispatch_operation(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.dispatch_operation(request, *args, **kwargs)

    def put(self, request, *args, **kwargs):
        return self.dispatch_operation(request, *args, **kwargs)

    def patch(self, request, *args, **kwargs):
        return self.dispatch_operation(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        return self.dispatch_operation(request, *args, **kwargs)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        response.headers.pop("ETag", None)
        return response


def import_view(operation):
    return type(
        f"DocumentImport{operation.replace('-', '_').title().replace('_', '')}View",
        (DocumentV3DispatchView,),
        {"handler_module": IMPORT_HANDLER, "operation": operation},
    ).as_view()


def read_view(operation):
    return type(
        f"DocumentRead{operation.replace('-', '_').title().replace('_', '')}View",
        (DocumentV3DispatchView,),
        {"handler_module": READ_HANDLER, "operation": operation},
    ).as_view()
