import pytest
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema


@pytest.mark.django_db
def test_private_file_openapi_contract():
    schema = SchemaGenerator().get_schema(request=None, public=True)
    validate_schema(schema)
    operation = schema["paths"]["/api/v1/prescription/{id}/file-url/"]["get"]
    assert {"BearerAuth": []} in operation["security"]
    assert {"tokenAuth": []} in operation["security"]
    response = operation["responses"]["200"]["content"]["application/json"]
    component = response["schema"]["$ref"].rsplit("/", 1)[1]
    properties = schema["components"]["schemas"][component]["properties"]
    assert set(properties) == {"url", "expires_at", "expires_in"}
    assert properties["expires_in"]["minimum"] == 1
    assert properties["expires_in"]["maximum"] == 900
    assert {"404", "503"}.issubset(operation["responses"])
