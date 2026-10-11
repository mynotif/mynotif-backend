from django.contrib.auth import get_user_model
from django.db import transaction
from djoser.views import UserViewSet as DjoserUserViewSet
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.throttling import ScopedRateThrottle


class AccountViewSet(DjoserUserViewSet):
    """Keep Djoser password flows while limiting user objects to their owner."""

    def get_queryset(self):
        return super().get_queryset().filter(pk=self.request.user.pk)

    def get_throttles(self):
        if self.action in ("create", "reset_password"):
            self.throttle_scope = (
                "registration" if self.action == "create" else "password_reset"
            )
            return [ScopedRateThrottle()]
        return super().get_throttles()

    @action(["post"], detail=False, url_path="set_username")
    def set_username(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method)

    @action(["post"], detail=False, url_path="reset_username")
    def reset_username(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method)

    @action(["post"], detail=False, url_path="reset_username_confirm")
    def reset_username_confirm(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method)

    @action(["post"], detail=False, url_path="set_password")
    def set_password(self, request, *args, **kwargs):
        from main.session_services import revoke_all

        user_model = get_user_model()
        with transaction.atomic():
            locked_user = user_model.objects.select_for_update().get(pk=request.user.pk)
            request.user = locked_user
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            locked_user.set_password(serializer.validated_data["new_password"])
            locked_user.save(update_fields=("password",))
            revoke_all(locked_user)
        return self._password_change_response(request, locked_user)

    @action(["post"], detail=False, url_path="reset_password_confirm")
    def reset_password_confirm(self, request, *args, **kwargs):
        from main.session_services import revoke_all

        user_model = get_user_model()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            locked_user = user_model.objects.select_for_update().get(
                pk=serializer.user.pk
            )
            request.user = locked_user
            locked_serializer = self.get_serializer(data=request.data)
            locked_serializer.is_valid(raise_exception=True)
            if locked_serializer.user.pk != locked_user.pk:
                raise MethodNotAllowed(request.method)
            locked_user.set_password(locked_serializer.validated_data["new_password"])
            if hasattr(locked_user, "last_login"):
                from django.utils.timezone import now

                locked_user.last_login = now()
            locked_user.save()
            revoke_all(locked_user)
        return self._password_change_response(request, locked_user)

    def _password_change_response(self, request, user):
        from djoser import utils
        from djoser.conf import settings
        from rest_framework.response import Response

        if settings.PASSWORD_CHANGED_EMAIL_CONFIRMATION:
            context = {"user": user}
            settings.EMAIL.password_changed_confirmation(request, context).send(
                [utils.get_user_email(user)]
            )
        if settings.LOGOUT_ON_PASSWORD_CHANGE:
            utils.logout_user(request)
        elif settings.CREATE_SESSION_ON_LOGIN:
            from django.contrib.auth import update_session_auth_hash

            update_session_auth_hash(request, user)
        return Response(status=204)
