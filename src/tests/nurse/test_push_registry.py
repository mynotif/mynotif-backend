"""Synthetic clients and ports are deliberately confined to the test package."""

import secrets
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.contrib.auth import get_user_model
from django.utils import timezone
from freezegun import freeze_time

from main.models import AuthSession
from main.session_services import authenticate_access, login, revoke, revoke_all
from nurse.push_crypto import b64encode, canonical
from nurse.push_custody import IngestionCustody, decrypt_for_transport
from nurse.push_interfaces import RegistryError
from nurse.push_models import (
    PushBinding,
    PushEnrollment,
    PushRegistryState,
    PushTokenVersion,
)
from nurse.push_services import Registry, invalidate_callback

pytestmark = pytest.mark.django_db
PASSWORD = "Synthetic-B07-test-only-237!"


class SyntheticKMS:
    def __init__(self):
        self.key = AESGCM.generate_key(bit_length=256)

    def wrap(self, key, aad):
        nonce = secrets.token_bytes(12)
        return nonce + AESGCM(self.key).encrypt(nonce, key, aad)

    def unwrap(self, wrapped, aad):
        return AESGCM(self.key).decrypt(wrapped[:12], wrapped[12:], aad)


class SyntheticControl:
    def __init__(self, kms):
        self.kms, self.deliveries = kms, []

    def deliver(self, **message):
        self.deliveries.append(message)
        # Proves the prepared envelope can be read only by the transport port.
        decrypt_for_transport(message["envelope"], message["aad"], self.kms)


class LocalClient:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pending = None

    @property
    def public(self):
        return b64encode(
            self.key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )

    def receive(self, enrollment_id, nonce):
        # A control for a different session/pending is ignored by the honest app.
        if not self.pending or self.pending["enrollment_id"] != enrollment_id:
            return None
        return b64encode(
            self.key.sign(
                canonical({**self.pending, "nonce": nonce}), ec.ECDSA(hashes.SHA256())
            )
        )


class Harness:
    def __init__(self):
        self.epoch = uuid.uuid4()
        PushRegistryState.objects.update_or_create(
            pk=1, defaults={"restore_epoch": self.epoch, "accepting": True}
        )
        self.kms = SyntheticKMS()
        self.control = SyntheticControl(self.kms)
        self.registry = Registry(
            restore_epoch=self.epoch,
            namespaces={"synthetic.android.dev": "fcm", "synthetic.ios.dev": "apns"},
            lookup_key=secrets.token_bytes(32),
            commitment_key=secrets.token_bytes(32),
            quota_key=secrets.token_bytes(32),
            custody=IngestionCustody(self.kms),
            control=self.control,
            emission_budget=1000,
        )

    def account(self, name=None):
        name = name or str(uuid.uuid4())
        user = get_user_model().objects.create_user(
            username=f"{name}@example.invalid",
            email=f"{name}@example.invalid",
            password=PASSWORD,
        )
        pair = login(user.email, PASSWORD)
        _, access = authenticate_access(pair["access_token"])
        return user, (access.session_id, access.pk), pair

    def enroll(self, auth, *, token="synthetic.destination.237", client=None, **extra):
        client = client or LocalClient()
        body = {
            "operation": "enroll",
            "request_id": str(uuid.uuid4()),
            "namespace": "synthetic.android.dev",
            "token": token,
            "public_key": client.public,
            "permission": "granted",
            **extra,
        }
        result, status = self.registry.enroll(auth, body, "192.0.2.1")
        assert status == 202
        client.pending = result["context"]
        return client, body, result

    def proof(self, client):
        delivery = next(
            m
            for m in self.control.deliveries
            if m["enrollment_id"] == client.pending["enrollment_id"]
        )
        return delivery["nonce"], client.receive(
            delivery["enrollment_id"], delivery["nonce"]
        )

    def confirm(self, auth, client):
        return self.registry.confirm(
            auth, client.pending["enrollment_id"], *self.proof(client)
        )


def error(code, function, *args, **kwargs):
    with pytest.raises(RegistryError) as exc:
        function(*args, **kwargs)
    assert exc.value.code == code
    return exc.value


def test_complete_proof_idempotence_and_lost_confirmation():
    h = Harness()
    user, auth, _ = h.account()
    client, body, pending = h.enroll(auth)
    assert "nonce" not in pending["context"]
    assert not PushBinding.objects.exists()
    replay, _ = h.registry.enroll(auth, body, "192.0.2.1")
    assert replay == pending and len(h.control.deliveries) == 1
    active, status = h.confirm(auth, client)
    assert status == 201 and active["selected"] is False
    recovered, status = h.confirm(auth, client)
    assert status == 200 and recovered == active
    assert PushTokenVersion.objects.count() == 1
    p = PushEnrollment.objects.get()
    assert p.commitment == p.public_key == "" and p.envelope == p.context == {}
    assert h.registry.bindings(auth)[0]["binding_id"] == active["binding_id"]
    error(
        "request_conflict",
        h.registry.enroll,
        auth,
        {**body, "token": "changed.synthetic"},
        "192.0.2.1",
    )


def test_A_to_B_rejects_old_and_fresh_A_without_reusing_credential():
    h = Harness()
    _, a, _ = h.account("A")
    _, b, _ = h.account("B")
    client_a, _, _ = h.enroll(a)
    binding_a, _ = h.confirm(a, client_a)
    now = timezone.now()
    with freeze_time(now + timedelta(seconds=61)):
        client_b, _, _ = h.enroll(b)
        binding_b, _ = h.confirm(b, client_b)
        error("superseded", h.confirm, a, client_a)
        assert h.registry.bindings(a) == []
        assert h.registry.bindings(b)[0]["binding_id"] == binding_b["binding_id"]
        assert binding_b["epoch"] > binding_a["epoch"]
    with freeze_time(now + timedelta(seconds=122)):
        fresh_a, _, _ = h.enroll(a)
        incoming = h.control.deliveries[-1]
        assert client_b.receive(incoming["enrollment_id"], incoming["nonce"]) is None
        # A knows its context/key/token but cannot learn the out-of-band nonce.
        guessed = b64encode(bytes(32))
        signature = fresh_a.receive(fresh_a.pending["enrollment_id"], guessed)
        error(
            "invalid_proof",
            h.registry.confirm,
            a,
            fresh_a.pending["enrollment_id"],
            guessed,
            signature,
        )
        error(
            "enrollment_not_found",
            h.registry.confirm,
            b,
            client_a.pending["enrollment_id"],
            *h.proof(client_a),
        )
        assert h.registry.bindings(b)[0]["binding_id"] == binding_b["binding_id"]


def test_new_enrollment_does_not_evict_an_earlier_valid_pending():
    h = Harness()
    _, a, _ = h.account()
    _, b, _ = h.account()
    first, _, _ = h.enroll(a)
    with freeze_time(timezone.now() + timedelta(seconds=61)):
        second, _, _ = h.enroll(b)
        h.confirm(a, first)
        error("superseded", h.confirm, b, second)


def test_rotation_preserves_old_until_proof_then_targets_callbacks():
    h = Harness()
    _, auth, _ = h.account()
    first, _, _ = h.enroll(auth)
    old, _ = h.confirm(auth, first)
    second, _, _ = h.enroll(
        auth,
        token="synthetic.new.destination",
        operation="rotate",
        previous_binding_id=old["binding_id"],
    )
    assert len(h.registry.bindings(auth)) == 1
    new, _ = h.confirm(auth, second)
    assert PushBinding.objects.get(pk=old["binding_id"]).revoked_at
    invalidate_callback(old["binding_id"], old["generation"])
    assert h.registry.bindings(auth)[0]["binding_id"] == new["binding_id"]
    assert invalidate_callback(uuid.uuid4(), 1) is False
    invalidate_callback(new["binding_id"], new["generation"] + 1)
    assert h.registry.bindings(auth)


@pytest.mark.parametrize(
    "action", ("logout", "all", "password", "delete", "inactive", "access_expired")
)
def test_authorization_rechecked_after_authentication(action):
    h = Harness()
    user, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    if action == "logout":
        revoke(AuthSession.objects.get(pk=auth[0]))
    elif action == "all":
        revoke_all(user)
    elif action == "password":
        user.set_password("changed-synthetic-password")
        user.save()
    elif action == "delete":
        user.delete()
    elif action == "inactive":
        user.is_active = False
        user.save()
    else:
        from main.models import AuthToken

        AuthToken.objects.filter(pk=auth[1]).update(
            issued_at=timezone.now() - timedelta(seconds=2),
            expires_at=timezone.now() - timedelta(seconds=1),
        )
    error(
        "not_authorized" if action == "inactive" else "invalid_session",
        h.confirm,
        auth,
        client,
    )
    assert not PushTokenVersion.objects.exists()


def test_reinstallation_state_loss_requires_new_key_and_proof():
    h = Harness()
    _, auth, _ = h.account()
    old_client, _, _ = h.enroll(auth)
    h.confirm(auth, old_client)
    with freeze_time(timezone.now() + timedelta(seconds=61)):
        error("fresh_key_required", h.enroll, auth, client=old_client)
        old_client.pending = None  # missing non-backed-up marker purges inherited state
        new_client, _, _ = h.enroll(auth)
        assert new_client.public != old_client.public
        assert (
            old_client.receive(
                new_client.pending["enrollment_id"], h.proof(new_client)[0]
            )
            is None
        )
        assert h.confirm(auth, new_client)[1] == 201


def test_control_failure_scrubs_pending_and_never_retries():
    h = Harness()
    _, auth, _ = h.account()
    with patch.object(
        h.control, "deliver", side_effect=RuntimeError("SYNTHETIC-SECRET-MARKER")
    ) as deliver:
        error("control_unavailable", h.enroll, auth)
        assert deliver.call_count == 1
    pending = PushEnrollment.objects.get()
    assert (
        pending.status == "abandoned"
        and pending.envelope == {}
        and pending.commitment == ""
    )
    assert not PushTokenVersion.objects.exists()


def test_account_delete_erases_credentials_and_identity_but_keeps_tombstone():
    h = Harness()
    user, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    h.confirm(auth, client)
    user.delete()
    binding = PushBinding.objects.get()
    assert (
        binding.owner_id is None and binding.session_id is None and binding.revoked_at
    )
    assert not PushEnrollment.objects.exists() and not PushTokenVersion.objects.exists()
