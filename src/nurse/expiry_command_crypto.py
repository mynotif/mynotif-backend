"""Versioned private keys and authenticated immutable correction intent."""

import base64
import hashlib
import hmac
import json
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

from nurse.expiry_state import ExpiryError


def canonical(payload):
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def keys(version):
    try:
        if type(version) is not str or not 1 <= len(version) <= 64:
            raise ValueError
        pair = settings.EXPIRY_COMMAND_KEYS[version]
        encryption, mac = (
            base64.b64decode(pair[name], validate=True)
            for name in ("encryption", "hmac")
        )
        if (
            len(encryption) != 32
            or len(mac) != 32
            or hmac.compare_digest(encryption, mac)
        ):
            raise ValueError
        return encryption, mac
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ExpiryError("service_unavailable") from None


def allocate_command_id(namespace, incarnation, epoch, key_version):
    """Separate allocation domains across restore, principal recreation and purge.

    Fresh entropy remains required. A new external namespace prevents replayed
    allocator entropy from selecting an old reference after a database restore.
    UUIDv4 keeps 122 PRF bits; collision resistance is cryptographic, not physical
    restore detection or a claim that a failed global RNG is safe.
    """
    _, mac = keys(key_version)
    allocation = canonical(
        {
            "namespace": str(namespace),
            "principal": str(incarnation),
            "epoch": str(epoch),
            "key": key_version,
            "entropy": os.urandom(32).hex(),
        }
    )
    material = hmac.new(
        mac, b"expiry-command-id-v1\0" + allocation, hashlib.sha256
    ).digest()
    return uuid.UUID(bytes=material[:16], version=4)


def associated_data(row):
    return canonical(
        {
            "principal": str(row.principal_incarnation),
            "user": row.user_id,
            "namespace": str(row.namespace),
            "epoch": str(row.mutation_epoch),
            "origin": str(row.origin_session_id),
            "kind": row.kind,
            "id": str(row.command_id),
            "key": row.key_version,
        }
    )


def digest(row, payload):
    _, mac = keys(row.key_version)
    return hmac.new(
        mac, associated_data(row) + b"\0" + canonical(payload), hashlib.sha256
    ).hexdigest()


def seal(row, payload):
    encryption, _ = keys(row.key_version)
    nonce = os.urandom(12)
    row.payload_hmac = digest(row, payload)
    row.payload_ciphertext = nonce + AESGCM(encryption).encrypt(
        nonce, canonical(payload), associated_data(row)
    )


def matches(row, payload):
    return hmac.compare_digest(row.payload_hmac, digest(row, payload))


def unseal(row):
    encryption, _ = keys(row.key_version)
    try:
        body = bytes(row.payload_ciphertext)
        payload = json.loads(
            AESGCM(encryption).decrypt(body[:12], body[12:], associated_data(row))
        )
        if not matches(row, payload):
            raise ValueError
        return payload
    except (InvalidTag, TypeError, ValueError, UnicodeError):
        raise ExpiryError("service_unavailable") from None
