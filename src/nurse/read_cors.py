from corsheaders.middleware import CorsMiddleware

from nurse.read_privacy import read_route


def private_read_methods(path):
    """Return the methods exposed by a private v3 endpoint."""
    if not path.startswith("/api/v3/"):
        return "GET, HEAD, OPTIONS"
    if path == "/api/v3/patients/" or path.endswith("/prescriptions/"):
        return "GET, HEAD, OPTIONS"
    if path == "/api/v3/document-capabilities/":
        return "GET, HEAD, OPTIONS"
    if path.endswith("/confirmation/"):
        return "HEAD, PUT, OPTIONS"
    if "/assets/" in path and path.endswith("/content/"):
        return "GET, HEAD, OPTIONS"
    if "/document-imports/" in path and path.endswith("/document-imports/"):
        return "GET, HEAD, POST, OPTIONS"
    if "/document-imports/" in path:
        return "GET, HEAD, PATCH, DELETE, OPTIONS"
    return "GET, HEAD, OPTIONS"


class PrivateReadCorsMiddleware(CorsMiddleware):
    def check_preflight(self, request):
        if read_route(request.path):
            # New reads require authentication/object rights even for OPTIONS.
            # Keep configured CORS response headers and legacy preflight behavior.
            request._cors_enabled = self.is_enabled(request)
            return None
        return super().check_preflight(request)

    def add_response_headers(self, request, response):
        response = super().add_response_headers(request, response)
        if read_route(request.path) and "Access-Control-Allow-Methods" in response:
            response["Access-Control-Allow-Methods"] = private_read_methods(
                request.path
            )
        return response
