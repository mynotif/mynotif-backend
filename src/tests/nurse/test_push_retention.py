from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from freezegun import freeze_time

from nurse.push_models import (
    PushBinding,
    PushEnrollment,
    PushQuota,
    PushRegistryState,
    PushTokenVersion,
)
from nurse.push_services import close_restored_registry, purge_registry
from tests.nurse.test_push_registry import Harness, error

pytestmark = pytest.mark.django_db


def test_nonce_ttl_and_purge_during_simulated_outage():
    h = Harness()
    _, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    with freeze_time(timezone.now() + timedelta(seconds=121)):
        error("proof_expired", h.confirm, auth, client)
        p = PushEnrollment.objects.get()
        assert p.envelope == {} and p.commitment == ""
        assert not PushTokenVersion.objects.exists()
    purge_registry(timezone.now() + timedelta(days=2))
    assert not PushEnrollment.objects.exists() and not PushQuota.objects.exists()


def test_session_expiry_closes_logical_use_before_purge_catches_up():
    h = Harness()
    _, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    h.confirm(auth, client)
    with freeze_time(timezone.now() + timedelta(days=31)):
        error("invalid_session", h.registry.bindings, auth)
        assert (
            PushTokenVersion.objects.count() == 1
        )  # delayed maintenance is not logical authority
        purge_registry()
        assert not PushTokenVersion.objects.exists()
        assert PushBinding.objects.get().revoked_at
    purge_registry(timezone.now() + timedelta(days=62))
    assert not PushBinding.objects.exists()


def test_confirmation_result_expires_without_reactivation():
    h = Harness()
    user, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    h.confirm(auth, client)
    future = timezone.now() + timedelta(hours=25)
    # A fresh access token of the SAME session, as after the ordinary refresh.
    from main.models import AuthToken

    AuthToken.objects.filter(pk=auth[1]).update(
        expires_at=future + timedelta(minutes=1)
    )
    with freeze_time(future):
        error("result_expired", h.confirm, auth, client)
        assert len(h.registry.bindings(auth)) == 1


def test_restore_erases_authority_closes_new_admissions_and_replay():
    h = Harness()
    _, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    h.confirm(auth, client)
    assert close_restored_registry() == {"state": "closed"}
    assert not PushRegistryState.objects.get().accepting
    assert not PushTokenVersion.objects.exists()
    error("registry_unavailable", h.confirm, auth, client)
    error("registry_unavailable", h.enroll, auth, token="new.synthetic")
    # Even an operator setting accepting alone cannot reuse the old epoch.
    PushRegistryState.objects.update(accepting=True)
    error("registry_unavailable", h.registry.bindings, auth)


def test_sliding_destination_quota_and_session_quota_do_not_evict():
    h = Harness()
    _, a, _ = h.account()
    _, b, _ = h.account()
    first, _, _ = h.enroll(a)
    error("rate_limited", h.enroll, b)
    assert h.confirm(a, first)[1] == 201
    for n in range(4):
        h.enroll(a, token=f"quota.synthetic.{n}")
    error("rate_limited", h.enroll, a, token="quota.synthetic.over")
    assert len(h.control.deliveries) == 5


def test_missing_configuration_fails_closed():
    from nurse.push_services import Registry

    h = Harness()
    kwargs = dict(
        restore_epoch=h.epoch,
        namespaces={},
        lookup_key=b"a" * 32,
        commitment_key=b"b" * 32,
        quota_key=b"c" * 32,
        custody=None,
        control=None,
        emission_budget=None,
    )
    error("registry_unavailable", Registry, **kwargs)


@pytest.mark.parametrize(
    "scope,limit", [("destination", 5), ("account", 20), ("ip", 60)]
)
def test_each_sliding_quota_is_enforced_before_control_emission(scope, limit):
    from main.session_services import authenticate_access, login
    from tests.nurse.test_push_registry import PASSWORD

    h = Harness()
    owner, _, _ = h.account()
    start = timezone.now()
    for n in range(limit + 1):
        with freeze_time(
            start + timedelta(seconds=61 * n if scope == "destination" else n)
        ):
            if scope == "account":
                pair = login(owner.email, PASSWORD)
                _, access = authenticate_access(pair["access_token"])
                auth = (access.session_id, access.pk)
            else:
                _, auth, _ = h.account()
            token = (
                "shared.synthetic" if scope == "destination" else f"quota.synthetic.{n}"
            )
            if n == limit:
                error("rate_limited", h.enroll, auth, token=token)
            else:
                h.enroll(auth, token=token)
    assert len(h.control.deliveries) == limit


@pytest.mark.parametrize("expires", ("access", "nonce"))
def test_expiry_during_crypto_is_rechecked_before_binding_commit(expires):
    from main.models import AuthToken
    from nurse import push_crypto

    h = Harness()
    _, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    nonce, signature = h.proof(client)
    now = timezone.now()
    if expires == "access":
        AuthToken.objects.filter(pk=auth[1]).update(
            expires_at=now + timedelta(seconds=1)
        )
    real_verify = push_crypto.verify
    with freeze_time(now) as clock:

        def delayed_verify(*args):
            real_verify(*args)
            clock.tick(timedelta(seconds=2 if expires == "access" else 121))

        with patch("nurse.push_crypto.verify", side_effect=delayed_verify):
            error(
                "invalid_session" if expires == "access" else "proof_expired",
                h.registry.confirm,
                auth,
                client.pending["enrollment_id"],
                nonce,
                signature,
            )
    assert not PushBinding.objects.exists()
    assert not PushTokenVersion.objects.exists()
