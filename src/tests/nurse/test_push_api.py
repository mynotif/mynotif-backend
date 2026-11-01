import logging
from unittest.mock import patch

import pytest
from django.test import override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from nurse.push_interfaces import before_breadcrumb, before_send
from nurse.push_services import get_registry
from tests.nurse.test_push_registry import Harness, LocalClient, error

pytestmark = pytest.mark.django_db
PATH = "/api/v2/push/enrollments/"


def test_actual_HTTP_assembly_stays_closed_even_with_flag():
    h = Harness()
    _, auth, pair = h.account()
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
    for enabled in (False, True):
        with override_settings(B07_API_ENABLED=enabled):
            response = client.post(PATH, {}, format="json")
            assert (
                response.status_code == 503
                and response["Cache-Control"] == "private, no-store"
            )
    error("registry_unavailable", get_registry)


@override_settings(B07_API_ENABLED=True)
def test_Bearer_only_and_strict_validation_under_synthetic_assembly():
    h = Harness()
    user, auth, pair = h.account()
    client = APIClient()
    with patch("nurse.push_views.get_registry", return_value=h.registry):
        assert client.post(PATH, {}, format="json").status_code == 401
        token = Token.objects.create(user=user)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        assert client.post(PATH, {}, format="json").status_code == 401
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
        response = client.post(
            PATH, {"owner": user.pk, "token": "SYNTHETIC-MARKER"}, format="json"
        )
        assert response.status_code == 400
        assert response.json() == {"code": "invalid_request"}


@override_settings(B07_API_ENABLED=True)
def test_HTTP_confirm_and_owner_isolation():
    h = Harness()
    _, a, pair_a = h.account()
    _, b, pair_b = h.account()
    device, body, pending = h.enroll(a)
    client = APIClient()
    with patch("nurse.push_views.get_registry", return_value=h.registry):
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair_b['access_token']}")
        detail = f"{PATH}{device.pending['enrollment_id']}/"
        assert client.get(detail).status_code == 404
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair_a['access_token']}")
        nonce, signature = h.proof(device)
        response = client.post(
            detail + "confirm/", {"nonce": nonce, "signature": signature}, format="json"
        )
        assert response.status_code == 201
        text = response.content.decode()
        assert body["token"] not in text and nonce not in text and signature not in text
        binding = response.json()["binding_id"]
        assert (
            client.get(f"/api/v2/push/bindings/{binding}/").json()["barrier_confirmed"]
            is False
        )
        unconfirmed = client.delete(f"/api/v2/push/bindings/{binding}/")
        assert unconfirmed.status_code == 503
        assert unconfirmed.json()["state"] == "revocation_barrier_unconfirmed"
        assert unconfirmed.json()["database_revoked"] is True
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair_b['access_token']}")
        assert client.delete(f"/api/v2/push/bindings/{binding}/").status_code == 404


@override_settings(B07_API_ENABLED=True)
def test_sensitive_markers_absent_from_errors_logs_and_APM(caplog):
    h = Harness()
    _, auth, pair = h.account()
    device = LocalClient()
    marker = "SYNTHETIC-SECRET-MARKER-237"

    def failing_port(**kwargs):
        logging.getLogger("b07.synthetic").error(
            marker, extra={"payload": kwargs}, exc_info=True
        )
        assert before_send({"exception": {"values": [{"value": marker}]}}, {}) is None
        assert before_breadcrumb({"message": marker}, {}) is None
        raise RuntimeError(marker)

    with (
        caplog.at_level(logging.DEBUG),
        patch.object(h.control, "deliver", side_effect=failing_port),
    ):
        error("control_unavailable", h.enroll, auth, token=marker, client=device)
    assert marker not in caplog.text
    assert all("payload" not in record.__dict__ for record in caplog.records)
    url = PATH + "?nonce=" + marker
    assert before_send({"request": {"url": url, "data": marker}}, {}) is None
    assert before_breadcrumb({"data": {"url": url, "headers": marker}}, {}) is None
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
    with patch("nurse.push_views.get_registry", return_value=h.registry):
        response = client.post(PATH, {"token": marker}, format="json")
    assert marker not in response.content.decode()
    # Development server access logging happens after middleware context reset.
    with caplog.at_level(logging.ERROR):
        logging.getLogger("django.server").error(
            "GET %s", url, extra={"payload": marker}
        )
    assert marker not in caplog.text
