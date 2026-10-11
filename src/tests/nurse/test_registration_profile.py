import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def generous_test_throttle_rates():
    cache.clear()
    with override_settings(
        REST_FRAMEWORK={
            **settings.REST_FRAMEWORK,
            "DEFAULT_THROTTLE_RATES": {
                "registration": "1000/min",
                "password_reset": "1000/min",
                "session_login": "1000/min",
                "session_refresh": "1000/min",
            },
        }
    ):
        yield
    cache.clear()


@pytest.mark.parametrize(
    "path,body",
    [
        (
            "/api/v1/account/register",
            {"username": "v1@example.invalid", "email": "v1@example.invalid"},
        ),
        ("/api/v2/account/register", {"email": "v2@example.invalid"}),
    ],
)
def test_registration_hashes_password_and_never_returns_it(path, body):
    response = APIClient().post(
        path, {**body, "password": "Synthetic-Strong-Pass-397!"}, format="json"
    )
    assert response.status_code == 201
    assert "password" not in response.data
    user = User.objects.get(email=body["email"])
    assert user.check_password("Synthetic-Strong-Pass-397!")
    assert hasattr(user, "nurse")


def test_profile_fields_are_read_only_and_password_rejected(user, authenticated_client):
    response = authenticated_client.patch(
        "/api/v1/user/current/",
        {
            "first_name": "Alice",
            "email": "changed@example.invalid",
            "is_staff": True,
        },
        format="json",
    )
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.first_name == "Alice"
    assert user.email != "changed@example.invalid"
    assert not user.is_staff
    response = authenticated_client.patch(
        "/api/v1/user/current/",
        {
            "first_name": "Ignored",
            "password": "Synthetic-Other-Pass-398!",
        },
        format="json",
    )
    assert response.status_code == 400
    user.refresh_from_db()
    assert user.first_name == "Alice"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/profile/",
        "/api/v1/user/current/",
        "/api/v1/auth/users/me/",
    ],
)
def test_profile_reads_keep_legacy_shape_without_password(
    path, user, authenticated_client
):
    response = authenticated_client.get(path)
    assert response.status_code == 200
    assert set(response.data) == {
        "id",
        "username",
        "email",
        "first_name",
        "last_name",
        "is_staff",
        "nurse",
    }
    assert "password" not in response.data


@pytest.mark.parametrize(
    "path",
    ["/api/v1/account/register", "/api/v2/account/register", "/api/v1/auth/users/"],
)
def test_registration_rejects_bad_email_and_weak_password(path):
    body = {"email": "not-an-email", "password": "password1"}
    if path != "/api/v2/account/register":
        body["username"] = "weak@example.invalid"
    response = APIClient().post(path, body, format="json")
    assert response.status_code == 400
    assert "password" in response.data or "email" in response.data
    assert "password1" not in str(response.data)


def test_v2_registration_rejects_client_username():
    response = APIClient().post(
        "/api/v2/account/register",
        {
            "username": "client-value",
            "email": "new@example.invalid",
            "password": "Synthetic-Strong-Pass-397!",
        },
        format="json",
    )
    assert response.status_code == 400


def test_djoser_username_change_routes_are_disabled(user, authenticated_client):
    for path in (
        "/api/v1/auth/users/set_username/",
        "/api/v1/auth/users/reset_username/",
        "/api/v1/auth/users/reset_username_confirm/",
    ):
        response = authenticated_client.post(path, {}, format="json")
        assert response.status_code == 405
    user.refresh_from_db()
    assert user.username == user.email


def test_djoser_password_change_revokes_opaque_and_legacy_tokens(
    user, authenticated_client
):
    from rest_framework.authtoken.models import Token

    from main.session_services import login

    legacy = Token.objects.get(user=user)
    session = login(user.email, "password1")
    response = authenticated_client.post(
        "/api/v1/auth/users/set_password/",
        {
            "current_password": "password1",
            "new_password": "Synthetic-Password-Changed-398!",
        },
        format="json",
    )
    assert response.status_code == 204
    assert not Token.objects.filter(pk=legacy.pk).exists()
    authenticated_client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {session['access_token']}"
    )
    assert authenticated_client.get("/api/v1/profile/").status_code == 401
