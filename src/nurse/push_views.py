"""Bearer-only D1 API. The production assembly remains deliberately closed."""

from django.conf import settings
from drf_spectacular.utils import OpenApiTypes, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from main.session_authentication import OpaqueSessionAuthentication
from nurse.push_interfaces import RegistryError
from nurse.push_serializers import ConfirmationSerializer, EnrollmentSerializer
from nurse.push_services import get_registry


class PushView(APIView):
    authentication_classes = [OpaqueSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # A flag alone cannot activate the HTTP flow: get_registry remains closed.
        if not getattr(settings, "B07_API_ENABLED", False):
            raise RegistryError()
        self.registry = get_registry()
        self.auth = (request.auth[0].pk, request.auth[1].pk)
        if len(request.body) > 8192:
            raise RegistryError("invalid_request", 400)

    def handle_exception(self, exc):
        if isinstance(exc, RegistryError):
            headers = {"Retry-After": "60"} if exc.status == 429 else {}
            return Response({"code": exc.code}, status=exc.status, headers=headers)
        response = super().handle_exception(exc)
        response.data = {
            "code": "invalid_request"
            if response.status_code == 400
            else "request_denied"
        }
        return response

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        # Explicit registry results are already allowlisted. Preserve the 503
        # unconfirmed-barrier state; unexpected Django DEBUG pages are scrubbed.
        response._b07_safe_error = True
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        return response


class EnrollmentView(PushView):
    @extend_schema(request=EnrollmentSerializer, responses={202: OpenApiTypes.OBJECT})
    def post(self, request):
        result, status = self.registry.enroll(
            self.auth, request.data, request.META.get("REMOTE_ADDR", "")
        )
        return Response(result, status=status)


class EnrollmentDetailView(PushView):
    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request, enrollment_id):
        return Response(self.registry.enrollment(self.auth, enrollment_id))


class ConfirmationView(PushView):
    @extend_schema(
        request=ConfirmationSerializer,
        responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT},
    )
    def post(self, request, enrollment_id):
        serializer = ConfirmationSerializer(data=request.data)
        if not serializer.is_valid():
            raise RegistryError("invalid_request", 400)
        result, status = self.registry.confirm(
            self.auth, enrollment_id, **serializer.validated_data
        )
        return Response(result, status=status)


class BindingCollectionView(PushView):
    @extend_schema(
        operation_id="b07_bindings_list", responses={200: OpenApiTypes.OBJECT}
    )
    def get(self, request):
        return Response({"bindings": self.registry.bindings(self.auth)})


class BindingDetailView(PushView):
    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request, binding_id):
        return Response(self.registry.binding_status(self.auth, binding_id))

    @extend_schema(
        responses={
            200: OpenApiTypes.OBJECT,
            202: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        }
    )
    def delete(self, request, binding_id):
        result, status = self.registry.revoke(self.auth, binding_id)
        return Response(result, status=status)
