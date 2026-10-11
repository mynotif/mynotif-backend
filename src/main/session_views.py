from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiTypes, extend_schema
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from main.models import AuthSession
from main.session_authentication import OpaqueSessionAuthentication
from main.session_serializers import (
    LoginSerializer,
    RefreshSerializer,
    SessionErrorSerializer,
    SessionOutputSerializer,
    SessionPairSerializer,
)
from main.session_services import SessionError, login, revoke, rotate


class SessionAPIError(APIException):
    messages = {
        "invalid_credentials": "Email ou mot de passe invalide.",
        "invalid_session": "Session invalide.",
        "invalid_token": "Session invalide.",
        "refresh_superseded": "Cette session a déjà été renouvelée.",
    }

    def __init__(self, error):
        self.status_code = error.status
        self.detail = {
            "code": error.code,
            "message": self.messages.get(error.code, "Authentification impossible."),
        }


class PrivateResponseMixin:
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        return response


class SessionCollectionView(PrivateResponseMixin, APIView):
    """POST creates a session; GET lists the caller's active sessions."""

    throttle_scope = "session_login"

    def initialize_request(self, request, *args, **kwargs):
        self._incoming_method = request.method
        return super().initialize_request(request, *args, **kwargs)

    def get_authenticators(self):
        if self._incoming_method == "POST":
            return []
        return [OpaqueSessionAuthentication()]

    def get_permissions(self):
        if self._incoming_method == "POST":
            return [AllowAny()]
        return super().get_permissions()

    def get_throttles(self):
        if self._incoming_method == "POST":
            throttle = ScopedRateThrottle()
            throttle.scope = "session_login"
            return [throttle]
        return []

    @extend_schema(
        auth=[],
        request=LoginSerializer,
        responses={
            201: SessionPairSerializer,
            400: OpenApiTypes.OBJECT,
            401: SessionErrorSerializer,
            429: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            return Response(
                login(**serializer.validated_data), status=status.HTTP_201_CREATED
            )
        except SessionError as exc:
            raise SessionAPIError(exc) from exc

    @extend_schema(
        auth=[{"BearerAuth": []}],
        request=None,
        responses={200: SessionOutputSerializer(many=True), 401: OpenApiTypes.OBJECT},
    )
    def get(self, request):
        current = request.auth[0].pk
        sessions = AuthSession.objects.filter(
            user=request.user, revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).order_by("-created_at")
        return Response(
            [
                {
                    "session_id": item.pk,
                    "device_label": item.device_label,
                    "created_at": item.created_at,
                    "expires_at": item.expires_at,
                    "last_seen_at": item.last_seen_at,
                    "current": item.pk == current,
                }
                for item in sessions
            ]
        )


class SessionRefreshView(PrivateResponseMixin, APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = "session_refresh"
    throttle_classes = [ScopedRateThrottle]

    @extend_schema(
        auth=[],
        request=RefreshSerializer,
        responses={
            200: SessionPairSerializer,
            400: OpenApiTypes.OBJECT,
            401: SessionErrorSerializer,
            409: SessionErrorSerializer,
            429: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            return Response(rotate(**serializer.validated_data))
        except SessionError as exc:
            raise SessionAPIError(exc) from exc


class SessionCurrentView(PrivateResponseMixin, APIView):
    authentication_classes = [OpaqueSessionAuthentication]

    @extend_schema(request=None, responses={204: None, 401: OpenApiTypes.OBJECT})
    def delete(self, request):
        revoke(request.auth[0])
        return Response(status=status.HTTP_204_NO_CONTENT)


class SessionRevokeView(PrivateResponseMixin, APIView):
    authentication_classes = [OpaqueSessionAuthentication]

    @extend_schema(
        request=None,
        responses={204: None, 401: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
    def delete(self, request, session_id):
        session = get_object_or_404(AuthSession, pk=session_id, user=request.user)
        revoke(session)
        return Response(status=status.HTTP_204_NO_CONTENT)
