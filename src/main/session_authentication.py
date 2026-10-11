from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from main.session_services import authenticate_access


class OpaqueSessionAuthentication(BaseAuthentication):
    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != b"bearer":
            return None
        if len(parts) != 2:
            raise AuthenticationFailed("Invalid authorization header.")
        try:
            value = parts[1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise AuthenticationFailed("Invalid authorization header.") from exc
        result = authenticate_access(value)
        if result is None:
            raise AuthenticationFailed("Invalid access token.")
        user, token = result
        return user, (token.session, token)

    def authenticate_header(self, request):
        return "Bearer"
