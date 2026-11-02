"""D4 envelope boundary; this lot deliberately supplies no KMS implementation."""

import json
import secrets
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nurse.push_crypto import b64decode, b64encode


class KeyWrapper(Protocol):
    def wrap(self, key: bytes, aad: bytes) -> bytes: ...


class KeyUnwrapper(Protocol):
    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes: ...


def associated_data(namespace, binding_id, generation, restore_epoch):
    return json.dumps(
        [1, namespace, str(binding_id), generation, str(restore_epoch)],
        separators=(",", ":"),
    ).encode("ascii")


class IngestionCustody:
    """Encryption-only capability. Ingestion never receives unwrap authority."""

    def __init__(self, wrapper: KeyWrapper):
        self._wrapper = wrapper

    def seal(self, token, aad):
        key, nonce = AESGCM.generate_key(bit_length=256), secrets.token_bytes(12)
        return {
            "v": 1,
            "nonce": b64encode(nonce),
            "ciphertext": b64encode(
                AESGCM(key).encrypt(nonce, token.encode("ascii"), aad)
            ),
            "wrapped_key": b64encode(self._wrapper.wrap(key, aad)),
        }


def decrypt_for_transport(envelope, aad, unwrapper: KeyUnwrapper):
    """Primitive for a future admitted transport, never called by registry/API.

    The caller must already hold D2 admission and generation authorisation. No
    provider, credentials, production unwrapper or invocation exists in this lot.
    """
    if envelope.get("v") != 1:
        raise ValueError("custody_unavailable")
    try:
        key = unwrapper.unwrap(b64decode(envelope["wrapped_key"], 4096), aad)
        nonce = b64decode(envelope["nonce"], 12)
        if len(key) != 32 or len(nonce) != 12:
            raise ValueError("custody_unavailable")
        return (
            AESGCM(key)
            .decrypt(
                nonce,
                b64decode(envelope["ciphertext"], 8192),
                aad,
            )
            .decode("ascii")
        )
    except Exception:
        raise ValueError("custody_unavailable") from None
