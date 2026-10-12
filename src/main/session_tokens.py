import base64
import hashlib
import hmac
import re
import secrets
import uuid

TOKEN_RE = re.compile(
    r"\A([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-"
    r"[0-9a-f]{12})\.([A-Za-z0-9_-]{43})\Z"
)


def generate_secret():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def hash_secret(secret):
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def parse_token(value):
    if not isinstance(value, str):
        return None
    match = TOKEN_RE.fullmatch(value)
    if not match:
        return None
    try:
        token_id = uuid.UUID(match.group(1))
        encoded = match.group(2)
        decoded = base64.urlsafe_b64decode(encoded + "=")
        if len(decoded) != 32:
            return None
        if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != encoded:
            return None
        return token_id, encoded
    except ValueError:
        return None


def verify_secret(secret, expected_hash):
    return hmac.compare_digest(hash_secret(secret), expected_hash)


def hash_request_id(request_id):
    return hashlib.sha256(request_id.bytes).hexdigest()


def recovery_secret(parent_secret, request_id, token_id, kind):
    key = base64.urlsafe_b64decode(parent_secret + "=")
    message = (
        b"ordopro:b01:rotation:v1:"
        + kind.encode("ascii")
        + b":"
        + token_id.bytes
        + request_id.bytes
    )
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
