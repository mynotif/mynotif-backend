import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def test_login_refresh_and_bearer_only_session_routes():
    user = User.objects.create_user(
        username="api-session@example.invalid",
        email="api-session@example.invalid",
        password="Synthetic-Session-Pass-397!",
    )
    client = APIClient()
    response = client.post(
        "/api/v2/auth/sessions/",
        {
            "email": user.email,
            "password": "Synthetic-Session-Pass-397!",
            "device_label": "tablet",
        },
        format="json",
    )
    assert response.status_code == 201
    assert response["Cache-Control"] == "private, no-store"
    pair = response.data
    assert "password" not in response.data
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
    assert client.get("/api/v2/auth/sessions/").status_code == 200
    assert client.get("/api/v1/profile/").status_code == 200
    client.credentials(HTTP_AUTHORIZATION=f"Token {pair['access_token']}")
    assert client.get("/api/v2/auth/sessions/").status_code == 401


def test_unknown_login_has_generic_error():
    response = APIClient().post(
        "/api/v2/auth/sessions/",
        {"email": "unknown@example.invalid", "password": "Synthetic-Session-Pass-397!"},
        format="json",
    )
    assert response.status_code == 401
    assert set(response.data) == {"code", "message"}
    assert response.data["code"] == "invalid_credentials"


def test_session_login_attempts_are_rate_limited():
    cache.clear()
    try:
        client = APIClient()
        statuses = [
            client.post(
                "/api/v2/auth/sessions/",
                {"email": "unknown@example.test", "password": "Synthetic-Wrong!"},
                format="json",
            ).status_code
            for _ in range(6)
        ]
        assert statuses == [401] * 5 + [429]
    finally:
        cache.clear()
