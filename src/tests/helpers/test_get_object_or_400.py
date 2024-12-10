import pytest
from django.contrib.auth.models import User
from django.core.exceptions import BadRequest

from helpers.model_utils import get_object_or_400


@pytest.mark.django_db
def test_get_object_or_400():
    user = User.objects.create_user(username="testuser", password="password123")

    retrieved_user = get_object_or_400(User, username="testuser")
    assert retrieved_user == user

    with pytest.raises(BadRequest):
        get_object_or_400(User, username="nonexistentuser")
