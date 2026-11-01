"""D1 wire primitives. No logging, normalisation, or secret-bearing exceptions."""

import base64
import hashlib
import hmac
import re
import struct
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

DOMAIN = b"ORDOPRO-B07-CONTROL-V1"
FIELDS = (
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
)


class InvalidProof(ValueError):
    def __init__(self):
        super().__init__("invalid_proof")


def b64encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64decode(value, maximum=256):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise InvalidProof()
    if len(value) > maximum * 2:
        raise InvalidProof()
    try:
        result = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except ValueError:
        raise InvalidProof() from None
    if len(result) > maximum or b64encode(result) != value:
        raise InvalidProof()
    return result


def public_key(value):
    raw = b64decode(value, 65)
    if len(raw) != 65 or raw[0] != 4:
        raise InvalidProof()
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        raise InvalidProof() from None


def digest(value):
    return hashlib.sha256(value).hexdigest()


def keyed_digest(key, *values):
    encoded = b"".join(struct.pack(">I", len(v)) + v for v in values)
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def canonical(context):
    if set(context) != set(FIELDS) or any(
        not isinstance(v, str) for v in context.values()
    ):
        raise InvalidProof()
    if context["operation"] not in ("enroll", "rotate"):
        raise InvalidProof()
    for field in ("enrollment_id", "request_id", "account_ref", "session_ref"):
        try:
            if str(uuid.UUID(context[field])) != context[field]:
                raise InvalidProof()
        except (ValueError, AttributeError):
            raise InvalidProof() from None
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", context["namespace"]):
        raise InvalidProof()
    for field in ("token_digest", "public_key_digest"):
        if not re.fullmatch(r"[0-9a-f]{64}", context[field]):
            raise InvalidProof()
    for field in ("expected_epoch", "expected_generation", "expires_at"):
        if not re.fullmatch(r"0|[1-9][0-9]{0,18}", context[field]):
            raise InvalidProof()
    if len(b64decode(context["nonce"], 32)) != 32:
        raise InvalidProof()
    return DOMAIN + b"".join(
        struct.pack(">I", len(context[field].encode("utf-8")))
        + context[field].encode("utf-8")
        for field in FIELDS
    )


def verify(key, signature, context):
    try:
        raw = b64decode(signature, 72)
        r, s = utils.decode_dss_signature(raw)
        if utils.encode_dss_signature(r, s) != raw:
            raise InvalidProof()
        public_key(key).verify(raw, canonical(context), ec.ECDSA(hashes.SHA256()))
    except (ValueError, InvalidSignature):
        raise InvalidProof() from None
