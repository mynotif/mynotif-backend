import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.db import close_old_connections, connection
from djoser import utils
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from main.models import AuthSession, AuthToken
from main.session_services import login, rotate

pytestmark = [pytest.mark.django_db(transaction=True)]


def test_concurrent_identical_refresh_returns_same_pair_and_single_children():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks are required")
    user = User.objects.create_user(
        username="concurrent@example.invalid",
        email="concurrent@example.invalid",
        password="Synthetic-Session-Pass-397!",
    )
    first = login(user.email, "Synthetic-Session-Pass-397!")
    request_id = uuid.uuid4()
    barrier = threading.Barrier(2)

    def refresh():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return rotate(first["refresh_token"], request_id)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = list(pool.map(lambda _: refresh(), range(2)))
    assert {key: value for key, value in one.items() if key != "expires_in"} == {
        key: value for key, value in two.items() if key != "expires_in"
    }
    assert abs(one["expires_in"] - two["expires_in"]) <= 1
    session = AuthSession.objects.get(pk=first["session_id"])
    assert AuthToken.objects.filter(session=session, kind="refresh").count() == 2


def test_concurrent_replay_with_different_ids_revokes_after_commit():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks are required")
    user = User.objects.create_user(
        username="replay@example.invalid",
        email="replay@example.invalid",
        password="Synthetic-Session-Pass-397!",
    )
    first = login(user.email, "Synthetic-Session-Pass-397!")
    barrier = threading.Barrier(2)

    def refresh():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return rotate(first["refresh_token"], uuid.uuid4())
        except Exception as exc:
            return exc
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: refresh(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert AuthSession.objects.get(pk=first["session_id"]).revoked_at is not None


def test_concurrent_registration_enforces_unique_login():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL uniqueness race is required")
    barrier = threading.Barrier(2)

    def register():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            response = APIClient().post(
                "/api/v2/account/register",
                {
                    "email": "registration-race@example.invalid",
                    "password": "Synthetic-Session-Pass-397!",
                },
                format="json",
            )
            return response.status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _: register(), range(2)))
    assert statuses == [201, 400]
    assert (
        User.objects.filter(username="registration-race@example.invalid").count() == 1
    )


def test_concurrent_password_change_and_login_cannot_issue_old_password_session():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks are required")
    user = User.objects.create_user(
        username="password-race@example.invalid",
        email="password-race@example.invalid",
        password="Synthetic-Session-Pass-397!",
    )
    token = Token.objects.create(user=user)
    barrier = threading.Barrier(2)

    def login_old_password():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return login(user.email, "Synthetic-Session-Pass-397!")
            except Exception as exc:
                return exc
        finally:
            close_old_connections()

    def change_password():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
            response = client.post(
                "/api/v1/auth/users/set_password/",
                {
                    "current_password": "Synthetic-Session-Pass-397!",
                    "new_password": "Synthetic-Session-Changed-398!",
                },
                format="json",
            )
            return response.status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(login_old_password), pool.submit(change_password)]
        login_result, change_result = [future.result() for future in results]
    assert change_result == 204
    if isinstance(login_result, dict):
        assert (
            AuthSession.objects.get(pk=login_result["session_id"]).revoked_at
            is not None
        )
    assert not AuthSession.objects.filter(user=user, revoked_at__isnull=True).exists()


def test_concurrent_password_reset_token_is_consumed_once():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks are required")
    user = User.objects.create_user(
        username="reset-race@example.invalid",
        email="reset-race@example.invalid",
        password="Synthetic-Session-Pass-397!",
    )
    body = {
        "uid": utils.encode_uid(user.pk),
        "token": default_token_generator.make_token(user),
        "new_password": "Synthetic-Reset-Password-398!",
    }
    barrier = threading.Barrier(2)

    def confirm_reset():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            response = APIClient().post(
                "/api/v1/auth/users/reset_password_confirm/", body, format="json"
            )
            return response.status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _: confirm_reset(), range(2)))
    assert statuses == [204, 400]
    user.refresh_from_db()
    assert user.check_password("Synthetic-Reset-Password-398!")
