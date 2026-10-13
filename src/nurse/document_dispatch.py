"""Versioned document API dispatch with Bearer-only authentication."""

import logging
from importlib import import_module

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
    409: ("invalid_state", "Cette opération ne peut pas être effectuée."),
    410: ("import_expired", "Ce brouillon a expiré."),
    412: ("revision_conflict", "Le brouillon a changé. Relisez-le."),
    413: ("document_too_large", "Le document dépasse la taille autorisée."),
    415: ("unsupported_media_type", "Format de document non pris en charge."),
    422: ("unreadable_document", "Le document ne peut pas être lu."),
    429: ("rate_limited", "Trop de demandes. Réessayez plus tard."),
    503: ("service_unavailable", "Service indisponible. Réessayez."),
}
_ERROR_MESSAGES = {
    **{code: message for code, message in _ERRORS.values()},
    "idempotency_conflict": "Cette clé correspond à un autre document.",
    "operation_in_progress": "Opération en cours. Réessayez.",
    "already_confirmed": "Cette ordonnance est déjà enregistrée.",
    "invalid_dates": "Les dates du document sont invalides.",
    "invalid_pages": "Les pages du document sont invalides.",
    "invalid_document": "Le document ne peut pas être accepté.",
    "encrypted_pdf": "Les PDF protégés ne sont pas acceptés.",
    "document_rejected": "Le document ne peut pas être accepté.",
    "validation_unavailable": "La validation est momentanément indisponible.",
}

IMPORT_HANDLER = "nurse.document_import_views"
READ_HANDLER = "nurse.document_read_views"

_IMPORT_METHODS = {
    "capabilities": ["get", "head", "options"],
    "imports": ["get", "post", "head", "options"],
    "import": ["get", "patch", "delete", "head", "options"],
    "confirmation": ["put", "options"],
    "import-asset-content": ["get", "head", "options"],
}
_READ_METHODS = {
    "prescriptions": ["get", "head", "options"],
    "prescription": ["get", "head", "options"],
    "document-manifest": ["get", "head", "options"],
    "prescription-document-content": ["get", "head", "options"],
}


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
        detail = response.data
        candidate_code = detail.get("code") if isinstance(detail, dict) else None
        if candidate_code in _ERROR_MESSAGES:
            code = candidate_code
            message = _ERROR_MESSAGES[code]
        else:
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

    def options(self, request, *args, **kwargs):
        # Capability handlers authorize the patient/object before returning the
        # route's method list. DRF's generic OPTIONS bypasses that object check.
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
        {
            "handler_module": IMPORT_HANDLER,
            "operation": operation,
            "http_method_names": _IMPORT_METHODS[operation],
        },
    ).as_view()


def read_view(operation):
    return type(
        f"DocumentRead{operation.replace('-', '_').title().replace('_', '')}View",
        (DocumentV3DispatchView,),
        {
            "handler_module": READ_HANDLER,
            "operation": operation,
            "http_method_names": _READ_METHODS[operation],
        },
    ).as_view()
