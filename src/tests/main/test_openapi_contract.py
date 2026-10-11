import pytest
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema


@pytest.mark.django_db
def test_accounts_sessions_openapi_contract_and_secret_response_boundaries(capsys):
    schema = SchemaGenerator().get_schema(request=None, public=True)
    validate_schema(schema)
    diagnostics = capsys.readouterr().err
    for view in (
        "SessionCollectionView",
        "SessionRefreshView",
        "SessionCurrentView",
        "SessionRevokeView",
        "ProfileView",
    ):
        assert f"Error [{view}" not in diagnostics

    paths = schema["paths"]
    collection = paths["/api/v2/auth/sessions/"]
    assert collection["get"]["security"] == [{"BearerAuth": []}]
    assert not collection["post"].get("security")

    def response_properties(operation, status):
        response = operation["responses"][status]["content"]["application/json"]
        reference = response["schema"]["$ref"].rsplit("/", 1)[1]
        return schema["components"]["schemas"][reference]["properties"]

    pair = response_properties(collection["post"], "201")
    assert set(pair) == {
        "session_id",
        "access_token",
        "refresh_token",
        "token_type",
        "access_expires_at",
        "expires_in",
        "refresh_expires_at",
        "session_expires_at",
    }
    refresh = paths["/api/v2/auth/sessions/refresh/"]["post"]
    assert response_properties(refresh, "200") == pair
    assert not refresh.get("security")
    for path in (
        "/api/v2/auth/sessions/current/",
        "/api/v2/auth/sessions/{session_id}/",
    ):
        assert "content" not in paths[path]["delete"]["responses"]["204"]

    assert "password" not in response_properties(
        paths["/api/v1/profile/"]["get"], "200"
    )
    for path in ("/api/v1/account/register", "/api/v2/account/register"):
        assert "password" not in response_properties(paths[path]["post"], "201")
