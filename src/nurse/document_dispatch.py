"""Versioned document API dispatch with Bearer-only authentication."""

from importlib import import_module

from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from main.session_authentication import OpaqueSessionAuthentication

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
