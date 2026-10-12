import base64
import hashlib
import hmac
import uuid

from main.session_tokens import (
    generate_secret,
    hash_secret,
    parse_token,
    recovery_secret,
    verify_secret,
)


def test_token_parser_and_secret_helpers():
    token_id = uuid.uuid4()
    secret = generate_secret()
    value = f"{token_id}.{secret}"
    assert parse_token(value) == (token_id, secret)
    assert parse_token(value + "x") is None
    assert parse_token(f"{uuid.uuid1()}.{secret}") is None
    assert verify_secret(secret, hash_secret(secret))
    assert not verify_secret(secret, hash_secret(generate_secret()))


def test_recovery_hmac_is_deterministic_and_domain_separated(settings):
    request_id, token_id = uuid.uuid4(), uuid.uuid4()
    parent_secret = generate_secret()
    parent_key = base64.urlsafe_b64decode(parent_secret + "=")
    message = b"ordopro:b01:rotation:v1:access:" + token_id.bytes + request_id.bytes
    expected = (
        base64.urlsafe_b64encode(hmac.new(parent_key, message, hashlib.sha256).digest())
        .rstrip(b"=")
        .decode()
    )
    one = recovery_secret(parent_secret, request_id, token_id, "access")
    assert len(parent_key) == 32
    assert one == expected
    assert one == recovery_secret(parent_secret, request_id, token_id, "access")
    assert one != recovery_secret(parent_secret, request_id, token_id, "refresh")
    assert one != recovery_secret(generate_secret(), request_id, token_id, "access")
