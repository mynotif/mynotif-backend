from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group, Permission, User
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


@pytest.fixture
def account_rights():
    return Group.objects.create(name="Test group"), Permission.objects.first()


def account_fields(group, permission, enabled=True):
    return {
        "is_staff": enabled,
        "is_superuser": enabled,
        "groups": [group.pk] if enabled else [],
        "user_permissions": [permission.pk] if enabled else [],
    }


def assert_standard_account(user):
    user.refresh_from_db()
    assert not user.is_staff
    assert not user.is_superuser
    assert not user.groups.exists()
    assert not user.user_permissions.exists()


def assert_admin_action_denied(client, monkeypatch):
    notify = Mock()
    monkeypatch.setattr("nurse.views.notify", notify)
    assert client.post("/api/v1/notify/").status_code == 403
    notify.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/account/register",
        "/api/v2/account/register",
        "/api/v1/auth/users/",
    ],
)
def test_registration_preserves_server_managed_fields(url, account_rights, monkeypatch):
    group, permission = account_rights
    client = APIClient()
    response = client.post(
        url,
        {
            "username": "new@example.test",
            "email": "new@example.test",
            "password": "Synthetic-test-password-39!",
            **account_fields(group, permission),
        },
        format="json",
    )
    assert response.status_code == 201
    user = User.objects.get(email="new@example.test")
    assert_standard_account(user)
    assert "password" not in response.data
    if "account/register" in url:
        assert response.data["is_staff"] is False
    client.force_authenticate(user=user)
    assert_admin_action_denied(client, monkeypatch)


@pytest.mark.parametrize("method", ["put", "patch"])
@pytest.mark.parametrize("route", ["custom", "djoser_me", "djoser_detail"])
@pytest.mark.parametrize("administrator", [False, True])
def test_update_preserves_account_rights(
    user, account_rights, method, route, administrator, monkeypatch
):
    group, permission = account_rights
    if administrator:
        user.is_staff = True
        user.is_superuser = True
        user.save()
        user.groups.add(group)
        user.user_permissions.add(permission)
    client = APIClient()
    client.force_authenticate(user=user)
    urls = {
        "custom": "/api/v1/user/current/",
        "djoser_me": "/api/v1/auth/users/me/",
        "djoser_detail": f"/api/v1/auth/users/{user.pk}/",
    }
    response = getattr(client, method)(
        urls[route],
        {
            "username": user.username,
            "email": "updated@example.test",
            "password": "Synthetic-test-password-39!",
            **account_fields(group, permission, enabled=not administrator),
        },
        format="json",
    )
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.email == "updated@example.test"
    assert "password" not in response.data
    if route == "custom":
        assert response.data["is_staff"] is administrator
    if administrator:
        assert user.is_staff
        assert user.is_superuser
        assert list(user.groups.all()) == [group]
        assert list(user.user_permissions.all()) == [permission]
    else:
        assert_standard_account(user)
        assert_admin_action_denied(client, monkeypatch)
