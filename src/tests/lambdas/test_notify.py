from unittest import mock

import pytest
from rest_framework import status

from lambdas import notify


def patch_post(json_response=None, text_response="", status_code=status.HTTP_200_OK):
    mock_response = mock.Mock(
        json=lambda: json_response, text=text_response, status_code=status_code
    )
    mock_post = mock.Mock(return_value=mock_response)
    return mock.patch("lambdas.notify.requests.post", mock_post)


@pytest.mark.parametrize(
    "env_value,expected_exception,exception_msg",
    [
        ({"BACKEND_URL": ""}, AssertionError, "BACKEND_URL must be set"),
    ],
)
def test_backend_url(env_value, expected_exception, exception_msg):
    with (
        mock.patch.dict("os.environ", env_value),
        pytest.raises(expected_exception, match=exception_msg),
    ):
        notify.backend_url()


def test_authenticate():
    mock_json = {"token": "testtoken"}
    with (
        mock.patch.dict(
            "os.environ",
            {
                "NOTIFY_USERNAME": "username",
                "NOTIFY_PASSWORD": "password",
                "BACKEND_URL": "http://testurl.com",
            },
        ),
        patch_post(mock_json) as mock_post,
    ):
        assert notify.authenticate() == mock_json["token"]
    assert mock_post.call_count == 1


@pytest.mark.parametrize(
    "env_values,expected_exception,exception_msg",
    [
        (
            {"NOTIFY_USERNAME": "", "NOTIFY_PASSWORD": ""},
            AssertionError,
            "NOTIFY_USERNAME must be set",
        ),
        (
            {"NOTIFY_USERNAME": "username", "NOTIFY_PASSWORD": ""},
            AssertionError,
            "NOTIFY_PASSWORD must be set",
        ),
    ],
)
def test_authenticate_error(env_values, expected_exception, exception_msg):
    with (
        mock.patch.dict("os.environ", env_values),
        pytest.raises(expected_exception, match=exception_msg),
    ):
        notify.authenticate()


def test_notify():
    status_code = status.HTTP_204_NO_CONTENT
    body = "body"
    with (
        mock.patch.dict(
            "os.environ",
            {
                "BACKEND_URL": "http://testurl.com",
            },
        ),
        patch_post(status_code=status_code, text_response=body) as mock_post,
    ):
        result = notify.notify("testtoken")
    assert mock_post.call_count == 1
    assert result == {"statusCode": status_code, "body": body}


def test_notify_error():
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "An error message"
    with (
        mock.patch.dict(
            "os.environ",
            {
                "BACKEND_URL": "http://testurl.com",
            },
        ),
        patch_post(status_code=status_code, text_response=message) as mock_post,
        pytest.raises(AssertionError, match=rf"\({status_code}, '{message}'\)"),
    ):
        notify.notify("testtoken")
    assert mock_post.call_count == 1


def test_handler():
    token = "testtoken"
    with (
        mock.patch(
            "lambdas.notify.authenticate", return_value=token
        ) as mock_authenticate,
        mock.patch(
            "lambdas.notify.notify", return_value={"statusCode": 204, "body": ""}
        ) as mock_notify,
    ):
        notify.handler({}, None)
    mock_authenticate.call_count == 1
    mock_notify.call_args_list == [mock.call(token)]
