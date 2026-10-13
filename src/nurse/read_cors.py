from corsheaders.middleware import CorsMiddleware

from nurse.read_privacy import read_route


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
            response["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        return response
