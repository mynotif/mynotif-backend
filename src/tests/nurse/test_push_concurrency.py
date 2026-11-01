import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.db import close_old_connections, connection
from django.utils import timezone
from djoser import utils
from freezegun import freeze_time
from rest_framework.test import APIClient

from main.models import AuthSession
from main.session_services import authenticate_access, revoke, revoke_all
from nurse.push_interfaces import RegistryError
from nurse.push_models import PushBinding, PushTokenVersion
from tests.nurse.test_push_registry import Harness

pytestmark = pytest.mark.django_db(transaction=True)


def require_postgres():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks are required")


def worker(function):
    close_old_connections()
    try:
        try:
            return function()
        except RegistryError as exc:
            return exc.code
    finally:
        close_old_connections()


def test_concurrent_confirmation_has_one_CAS_winner():
    require_postgres()
    h = Harness()
    _, a, _ = h.account()
    _, b, _ = h.account()
    one, _, _ = h.enroll(a)
    with freeze_time(timezone.now() + timedelta(seconds=61)):
        two, _, _ = h.enroll(b)
        start = threading.Barrier(2)

        def confirm(auth, client):
            start.wait(timeout=10)
            return h.confirm(auth, client)[1]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(worker, lambda: confirm(a, one)),
                pool.submit(worker, lambda: confirm(b, two)),
            ]
            results = [f.result(timeout=15) for f in futures]
    assert results.count(201) == 1 and results.count("superseded") == 1
    assert PushBinding.objects.filter(revoked_at__isnull=True).count() == 1
    assert PushTokenVersion.objects.count() == 1


def test_concurrent_same_confirmation_recovers_same_result():
    require_postgres()
    h = Harness()
    _, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    start = threading.Barrier(2)

    def confirm():
        start.wait(timeout=10)
        return h.confirm(auth, client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, confirm) for _ in range(2)]
        results = [f.result(timeout=15) for f in futures]
    assert sorted(r[1] for r in results) == [200, 201]
    assert results[0][0] == results[1][0]
    assert PushBinding.objects.count() == 1


def test_concurrent_initial_request_sends_control_once():
    require_postgres()
    import uuid

    from tests.nurse.test_push_registry import LocalClient

    h = Harness()
    _, auth, _ = h.account()
    body = {
        "operation": "enroll",
        "request_id": str(uuid.uuid4()),
        "namespace": "synthetic.android.dev",
        "token": "synthetic.concurrent",
        "public_key": LocalClient().public,
        "permission": "granted",
    }
    start = threading.Barrier(2)

    def enroll():
        start.wait(timeout=10)
        return h.registry.enroll(auth, body, "192.0.2.1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, enroll) for _ in range(2)]
        results = [f.result(timeout=15) for f in futures]
    assert results[0] == results[1]
    assert len(h.control.deliveries) == 1


@pytest.mark.parametrize("action", ("logout", "revoke_all", "reset", "delete"))
def test_revocation_between_authentication_and_commit_wins(action):
    require_postgres()
    h = Harness()
    user, auth, pair = h.account()
    client, _, _ = h.enroll(auth)
    authenticated, proceed = threading.Event(), threading.Event()

    def delayed_confirmation():
        assert authenticate_access(pair["access_token"]) is not None
        authenticated.set()
        assert proceed.wait(10)
        return h.confirm(auth, client)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker, delayed_confirmation)
        assert authenticated.wait(10)
        if action == "logout":
            revoke(AuthSession.objects.get(pk=auth[0]))
        elif action == "revoke_all":
            revoke_all(user)
        elif action == "reset":
            response = APIClient().post(
                "/api/v1/auth/users/reset_password_confirm/",
                {
                    "uid": utils.encode_uid(user.pk),
                    "token": default_token_generator.make_token(user),
                    "new_password": "Synthetic-Changed-237-Reset!",
                },
                format="json",
            )
            assert response.status_code == 204
        else:
            user.delete()
        proceed.set()
        assert future.result(timeout=15) == "invalid_session"
    assert not PushBinding.objects.filter(revoked_at__isnull=True).exists()
    assert not PushTokenVersion.objects.exists()


def test_confirmation_already_holding_user_lock_is_revoked_after_its_commit():
    require_postgres()
    h = Harness()
    _, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    locked, proceed, revoking = threading.Event(), threading.Event(), threading.Event()
    original = h.registry._state

    def paused_state():
        locked.set()
        assert proceed.wait(10)
        return original()

    def logout():
        revoking.set()
        revoke(AuthSession.objects.get(pk=auth[0]))

    with (
        patch.object(h.registry, "_state", side_effect=paused_state),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        confirm = pool.submit(worker, lambda: h.confirm(auth, client))
        assert locked.wait(10)
        revoke_future = pool.submit(worker, logout)
        assert revoking.wait(10)
        proceed.set()
        assert confirm.result(timeout=15)[1] == 201
        assert revoke_future.result(timeout=15) is None
    assert PushBinding.objects.get().revoked_at
    assert not PushTokenVersion.objects.exists()
