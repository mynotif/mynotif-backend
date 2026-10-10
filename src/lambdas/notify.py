import os

import requests


def backend_url():
    assert (backend_url := os.environ.get("BACKEND_URL")), "BACKEND_URL must be set"
    return backend_url


def api_token_auth_url():
    return f"{backend_url()}/api-token-auth/"


def notify_url():
    return f"{backend_url()}/notify/"


def authenticate():
    assert (notify_username := os.environ.get("NOTIFY_USERNAME")), (
        "NOTIFY_USERNAME must be set"
    )
    assert (notify_password := os.environ.get("NOTIFY_PASSWORD")), (
        "NOTIFY_PASSWORD must be set"
    )
    url = api_token_auth_url()
    credentials = {
        "username": notify_username,
        "password": notify_password,
    }
    response = requests.post(url, data=credentials)
    token = response.json()["token"]
    return token


def notify(token: str):
    headers = {
        "Authorization": f"Token {token}",
    }
    url = notify_url()
    response = requests.post(url, headers=headers)
    assert response.status_code == 204, (response.status_code, response.text)
    return {"statusCode": response.status_code, "body": response.text}


def handler(event, context):
    token = authenticate()
    notify(token)


if __name__ == "__main__":
    event = {}
    context = None
    data = handler(event, context)
    print(f"{data=}")
