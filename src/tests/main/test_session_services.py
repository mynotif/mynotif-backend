import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from main.models import AuthSession, AuthToken
from main.session_services import (
    SessionError,
    authenticate_access,
    login,
    revoke_all,
    rotate,
)
from main.session_tokens import parse_token

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    user = User.objects.create_user(
        username="session@example.invalid",
        email="session@example.invalid",
        password="Synthetic-Session-Pass-397!",
    )
    return user


def test_absolute_expiry_and_refresh_recovery(account, monkeypatch):
    now = timezone.now()
    monkeypatch.setattr("main.session_services.timezone.now", lambda: now)
    first = login(account.email, "Synthetic-Session-Pass-397!", "synthetic")
    session = AuthSession.objects.get(pk=first["session_id"])
    assert session.expires_at == now + timedelta(days=30)
    request_id = uuid.uuid4()
    second = rotate(first["refresh_token"], request_id)
    parent_id, _ = parse_token(first["refresh_token"])
    parent = AuthToken.objects.get(pk=parent_id)
    assert parent.consumed_at == now
    assert second["session_expires_at"] == first["session_expires_at"]
    assert rotate(first["refresh_token"], request_id) == second
    assert AuthToken.objects.filter(session=session, kind="refresh").count() == 2


def test_replay_with_different_request_id_revokes_session(account):
    first = login(account.email, "Synthetic-Session-Pass-397!")
    rotate(first["refresh_token"], uuid.uuid4())
    with pytest.raises(SessionError) as error:
        rotate(first["refresh_token"], uuid.uuid4())
    assert error.value.code == "invalid_session"
    assert AuthSession.objects.get(pk=first["session_id"]).revoked_at is not None


def test_absolute_expiry_is_not_extended(account, monkeypatch):
    start = timezone.now()
    monkeypatch.setattr("main.session_services.timezone.now", lambda: start)
    first = login(account.email, "Synthetic-Session-Pass-397!")
    monkeypatch.setattr(
        "main.session_services.timezone.now",
        lambda: start + timedelta(days=29, hours=23, minutes=59),
    )
    second = rotate(first["refresh_token"], uuid.uuid4())
    assert second["session_expires_at"] == first["session_expires_at"]
    access = AuthToken.objects.get(
        kind="access", session__id=first["session_id"], generation=1
    )
    assert (
        access.expires_at == AuthSession.objects.get(pk=first["session_id"]).expires_at
    )


def test_wrong_refresh_secret_does_not_revoke(account):
    first = login(account.email, "Synthetic-Session-Pass-397!")
    token_id, secret = parse_token(first["refresh_token"])
    with pytest.raises(SessionError):
        rotate(f"{token_id}.{secret[:-1]}x", uuid.uuid4())
    assert AuthSession.objects.get(pk=first["session_id"]).revoked_at is None


def test_external_password_change_invalidates_access_and_revokes_session(account):
    first = login(account.email, "Synthetic-Session-Pass-397!")
    account.set_password("Synthetic-New-Pass-398!")
    account.save(update_fields=("password",))
    assert authenticate_access(first["access_token"]) is None
    assert AuthSession.objects.get(pk=first["session_id"]).revoked_at is not None


def test_revoke_all_invalidates_sessions_and_legacy_tokens(account):
    from rest_framework.authtoken.models import Token

    first = login(account.email, "Synthetic-Session-Pass-397!")
    legacy = Token.objects.create(user=account)
    revoke_all(account)
    assert AuthSession.objects.get(pk=first["session_id"]).revoked_at is not None
    assert not Token.objects.filter(pk=legacy.pk).exists()
