import hashlib
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from main.models import AuthSession, AuthToken
from main.session_tokens import (
    generate_secret,
    hash_request_id,
    hash_secret,
    recovery_secret,
    verify_secret,
)

User = get_user_model()
DUMMY_PASSWORD_HASH = make_password("not-a-real-account-password")


class SessionError(Exception):
    def __init__(self, code, status=401):
        self.code = code
        self.status = status
        super().__init__(code)


def fingerprint(user):
    return hashlib.sha256(user.password.encode("utf-8")).hexdigest()


def _pair(
    session,
    now,
    generation=0,
    recovered_parent=None,
    request_id=None,
    recovered_parent_secret=None,
    recovered_tokens=None,
):
    access_id, refresh_id = recovered_tokens or (uuid.uuid4(), uuid.uuid4())
    if recovered_parent is None:
        access_secret, refresh_secret = generate_secret(), generate_secret()
    else:
        access_secret = recovery_secret(
            recovered_parent_secret, request_id, access_id, "access"
        )
        refresh_secret = recovery_secret(
            recovered_parent_secret, request_id, refresh_id, "refresh"
        )
    access_ttl = min(
        int(getattr(settings, "AUTH_ACCESS_TTL_SECONDS", 900)),
        max(1, int((session.expires_at - now).total_seconds())),
    )
    if recovered_tokens:
        access = AuthToken.objects.get(pk=access_id)
        refresh = AuthToken.objects.get(pk=refresh_id)
        if not verify_secret(access_secret, access.token_hash) or not verify_secret(
            refresh_secret, refresh.token_hash
        ):
            raise SessionError("invalid_session")
    else:
        access = AuthToken.objects.create(
            id=access_id,
            session=session,
            kind=AuthToken.Kind.ACCESS,
            generation=generation,
            token_hash=hash_secret(access_secret),
            issued_at=now,
            expires_at=min(now + timedelta(seconds=access_ttl), session.expires_at),
        )
        refresh = AuthToken.objects.create(
            id=refresh_id,
            session=session,
            kind=AuthToken.Kind.REFRESH,
            generation=generation,
            token_hash=hash_secret(refresh_secret),
            issued_at=now,
            expires_at=session.expires_at,
        )
    return {
        "session_id": str(session.pk),
        "access_token": f"{access.pk}.{access_secret}",
        "refresh_token": f"{refresh.pk}.{refresh_secret}",
        "token_type": "Bearer",
        "access_expires_at": access.expires_at.isoformat(),
        "expires_in": max(0, int((access.expires_at - now).total_seconds())),
        "refresh_expires_at": session.expires_at.isoformat(),
        "session_expires_at": session.expires_at.isoformat(),
    }


def login(email, password, device_label=""):
    user = User.objects.filter(username__iexact=email).order_by("pk").first()
    if user is None:
        check_password(password, DUMMY_PASSWORD_HASH)
        raise SessionError("invalid_credentials")
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        if not user.is_active or not user.check_password(password):
            raise SessionError("invalid_credentials")
        now = timezone.now()
        session = AuthSession.objects.create(
            user=user,
            device_label=device_label[:80],
            created_at=now,
            expires_at=now + timedelta(seconds=settings.AUTH_SESSION_MAX_AGE_SECONDS),
            password_fingerprint=fingerprint(user),
            last_seen_at=now,
        )
        return _pair(session, now)


def _lock_context(token_id, secret, kind):
    token = (
        AuthToken.objects.filter(pk=token_id, kind=kind)
        .select_related("session")
        .first()
    )
    if token is None:
        raise SessionError("invalid_token")
    user = User.objects.select_for_update().get(pk=token.session.user_id)
    session = AuthSession.objects.select_for_update().get(pk=token.session_id)
    token = AuthToken.objects.select_for_update().get(pk=token_id)
    if not verify_secret(secret, token.token_hash):
        raise SessionError("invalid_token")
    return user, session, token


def _revoke_session(session, now):
    AuthSession.objects.filter(pk=session.pk, revoked_at__isnull=True).update(
        revoked_at=now
    )
    AuthToken.objects.filter(session=session, revoked_at__isnull=True).update(
        revoked_at=now
    )


def rotate(refresh_token, request_id):
    try:
        token_id, secret = __import__(
            "main.session_tokens", fromlist=["parse_token"]
        ).parse_token(refresh_token)
    except (TypeError, ValueError):
        token_id = secret = None
    if token_id is None:
        raise SessionError("invalid_token")
    parent = (
        AuthToken.objects.filter(pk=token_id, kind=AuthToken.Kind.REFRESH)
        .select_related("session")
        .first()
    )
    if parent is None:
        raise SessionError("invalid_token")
    if not isinstance(request_id, uuid.UUID) or request_id.version != 4:
        raise SessionError("invalid_request_id", 400)
    error = None
    result = None
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=parent.session.user_id)
        session = AuthSession.objects.select_for_update().get(pk=parent.session_id)
        parent = AuthToken.objects.select_for_update().get(pk=token_id)
        now = timezone.now()
        if not verify_secret(secret, parent.token_hash):
            error = SessionError("invalid_token")
        elif not user.is_active or session.revoked_at or session.expires_at <= now:
            error = SessionError("session_expired")
        elif session.password_fingerprint != fingerprint(user):
            _revoke_session(session, now)
            error = SessionError("session_expired")
        elif parent.consumed_at:
            if (
                parent.rotation_request_hash == hash_request_id(request_id)
                and parent.recovery_until
                and now < parent.recovery_until
            ):
                access = AuthToken.objects.get(
                    session=session,
                    kind=AuthToken.Kind.ACCESS,
                    generation=parent.generation + 1,
                )
                refresh = AuthToken.objects.get(
                    session=session,
                    kind=AuthToken.Kind.REFRESH,
                    generation=parent.generation + 1,
                )
                current_generation = (
                    AuthToken.objects.filter(
                        session=session, kind=AuthToken.Kind.REFRESH
                    )
                    .order_by("-generation")
                    .values_list("generation", flat=True)
                    .first()
                )
                if (
                    access.revoked_at is None
                    and refresh.revoked_at is None
                    and refresh.consumed_at is None
                    and current_generation == parent.generation + 1
                ):
                    result = _pair(
                        session,
                        now,
                        parent.generation + 1,
                        parent,
                        request_id,
                        recovered_parent_secret=secret,
                        recovered_tokens=(access.pk, refresh.pk),
                    )
                else:
                    error = SessionError("refresh_superseded", 409)
            else:
                _revoke_session(session, now)
                error = SessionError("invalid_session")
        elif (
            parent.revoked_at
            or parent.expires_at <= now
            or parent.generation
            != AuthToken.objects.filter(session=session, kind=AuthToken.Kind.REFRESH)
            .order_by("-generation")
            .values_list("generation", flat=True)
            .first()
        ):
            error = SessionError("invalid_token")
        else:
            parent.consumed_at = now
            parent.rotation_request_hash = hash_request_id(request_id)
            recovery_seconds = settings.AUTH_REFRESH_RECOVERY_SECONDS
            parent.recovery_until = min(
                now + timedelta(seconds=recovery_seconds), parent.expires_at
            )
            parent.save(
                update_fields=("consumed_at", "rotation_request_hash", "recovery_until")
            )
            AuthToken.objects.filter(
                session=session, kind=AuthToken.Kind.ACCESS, revoked_at__isnull=True
            ).update(revoked_at=now)
            session.last_seen_at = now
            session.save(update_fields=("last_seen_at",))
            result = _pair(
                session,
                now,
                parent.generation + 1,
                parent,
                request_id,
                recovered_parent_secret=secret,
            )
    if error:
        raise error
    return result


def authenticate_access(raw_token):
    from main.session_tokens import parse_token

    parsed = parse_token(raw_token)
    if not parsed:
        return None
    token_id, secret = parsed
    token = (
        AuthToken.objects.filter(pk=token_id, kind=AuthToken.Kind.ACCESS)
        .select_related("session", "session__user")
        .first()
    )
    if token is None or not verify_secret(secret, token.token_hash):
        return None
    now = timezone.now()
    session = token.session
    user = session.user
    if (
        token.revoked_at
        or token.expires_at <= now
        or session.revoked_at
        or session.expires_at <= now
        or not user.is_active
        or session.password_fingerprint != fingerprint(user)
    ):
        if session.password_fingerprint != fingerprint(user):
            _revoke_session(session, now)
        return None
    return user, token


def revoke(session, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        locked = AuthSession.objects.select_for_update().get(pk=session.pk)
        _revoke_session(locked, now)


def revoke_all(user):
    now = timezone.now()
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user.pk)
        AuthSession.objects.filter(user=locked, revoked_at__isnull=True).update(
            revoked_at=now
        )
        AuthToken.objects.filter(session__user=locked, revoked_at__isnull=True).update(
            revoked_at=now
        )
        from rest_framework.authtoken.models import Token

        Token.objects.filter(user=locked).delete()
