from drf_spectacular.extensions import OpenApiAuthenticationExtension


class OpaqueSessionAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "main.session_authentication.OpaqueSessionAuthentication"
    name = "BearerAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer"}
