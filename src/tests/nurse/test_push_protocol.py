import struct

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from nurse.push_crypto import (
    FIELDS,
    InvalidProof,
    b64decode,
    b64encode,
    canonical,
    verify,
)
from tests.nurse.test_push_registry import Harness, LocalClient, error

pytestmark = pytest.mark.django_db


def test_canonical_exact_bytes_and_independent_P256_signature():
    h = Harness()
    _, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    nonce, _ = h.proof(client)
    context = {**client.pending, "nonce": nonce}
    expected = b"ORDOPRO-B07-CONTROL-V1"
    for name in (
        "operation",
        "enrollment_id",
        "request_id",
        "account_ref",
        "session_ref",
        "namespace",
        "token_digest",
        "public_key_digest",
        "expected_epoch",
        "expected_generation",
        "expires_at",
        "nonce",
    ):
        field = context[name].encode("ascii")
        expected += struct.pack(">I", len(field)) + field
    assert canonical(context) == expected
    signature = b64encode(client.key.sign(expected, ec.ECDSA(hashes.SHA256())))
    verify(client.public, signature, context)
    for name in FIELDS:
        altered = {**context, name: context[name] + "x"}
        with pytest.raises(InvalidProof):
            verify(client.public, signature, altered)


@pytest.mark.parametrize(
    "mutation",
    ("trailing", "padding", "raw", "wrong_key", "truncated", "nonce", "unsigned"),
)
def test_invalid_proofs_and_strict_DER_are_rejected(mutation):
    h = Harness()
    _, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    nonce, signature = h.proof(client)
    if mutation == "trailing":
        signature = b64encode(b64decode(signature) + b"\x00")
    elif mutation == "padding":
        signature += "="
    elif mutation == "raw":
        signature = b64encode(bytes(64))
    elif mutation == "wrong_key":
        signature = b64encode(
            LocalClient().key.sign(
                canonical({**client.pending, "nonce": nonce}), ec.ECDSA(hashes.SHA256())
            )
        )
    elif mutation == "truncated":
        signature = signature[:-1]
    elif mutation == "nonce":
        nonce = b64encode(bytes(32))
    else:
        signature = ""
    error(
        "invalid_proof",
        h.registry.confirm,
        auth,
        client.pending["enrollment_id"],
        nonce,
        signature,
    )
    assert h.registry.bindings(auth) == []


def test_five_bad_confirmations_abandon_and_erase_proof():
    from nurse.push_models import PushEnrollment

    h = Harness()
    _, auth, _ = h.account()
    client, _, _ = h.enroll(auth)
    for _ in range(5):
        error(
            "invalid_proof",
            h.registry.confirm,
            auth,
            client.pending["enrollment_id"],
            "invalid",
            "invalid",
        )
    p = PushEnrollment.objects.get()
    assert (
        p.failures == 5
        and p.status == "abandoned"
        and p.commitment == ""
        and p.envelope == {}
    )
    error("proof_expired", h.confirm, auth, client)
