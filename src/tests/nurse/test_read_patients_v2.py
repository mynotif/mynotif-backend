from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.db import OperationalError, connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import Throttled
from rest_framework.test import APIClient

from main.models import AuthSession, AuthToken
from main.session_services import login, revoke, rotate
from nurse.models import Nurse, Patient
from nurse.read_api import MESSAGES
from nurse.read_views import PatientReadList
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db
URL = "/api/v2/patients/"
PATIENT_KEYS = {"id", "firstname", "lastname", "city", "phone", "birthday"}
PAGE_KEYS = {"count", "page", "page_size", "next_page", "previous_page", "results"}


@pytest.fixture
def read_context(user):
    pair = login(user.username, PASSWORD)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
    nurse = Nurse.objects.create(user=user)
    return client, nurse, pair


def assert_error(response, code):
    status, message = MESSAGES[code]
    assert response.status_code == status
    assert response.json() == {"code": code, "message": message}
    assert_private(response)
    if status == 401:
        assert response["WWW-Authenticate"] == "Bearer"


def assert_private(response):
    assert response["Cache-Control"] == "private, no-store"
    assert response["Pragma"] == "no-cache"
    assert "ETag" not in response
    assert response.status_code != 304
    assert response["Content-Type"].startswith("application/json")


def patient(nurse, **data):
    obj = Patient.objects.create(firstname="Fictif", lastname="Exemple", **data)
    nurse.patients.add(obj)
    return obj


@pytest.mark.parametrize(
    "auth", [None, "Bearer invalid", "Bearer", "Bearer a b", "Token invalid", "Basic a"]
)
@pytest.mark.parametrize("method", ["get", "head", "options", "post"])
def test_authentication_precedes_query_and_method(auth, method):
    client = APIClient()
    if auth:
        client.credentials(HTTP_AUTHORIZATION=auth)
    response = getattr(client, method)(URL + "?unknown=sensitive")
    if method == "head":
        assert response.status_code == 401
        assert response.content == b""
        assert_private(response)
        assert response["WWW-Authenticate"] == "Bearer"
    else:
        assert_error(response, "authentication_required")


@pytest.mark.parametrize(
    "state",
    [
        "expired",
        "revoked_token",
        "revoked_session",
        "expired_session",
        "inactive",
        "refresh",
        "historical",
    ],
)
def test_rejected_credentials(read_context, state):
    client, nurse, pair = read_context
    token_id = pair["access_token"].split(".")[0]
    past = timezone.now() - timedelta(seconds=1)
    if state == "expired":
        AuthToken.objects.filter(pk=token_id).update(
            expires_at=past, issued_at=past - timedelta(seconds=1)
        )
    elif state == "revoked_token":
        AuthToken.objects.filter(pk=token_id).update(revoked_at=timezone.now())
    elif state == "revoked_session":
        revoke(AuthSession.objects.get(pk=pair["session_id"]))
    elif state == "expired_session":
        AuthSession.objects.filter(pk=pair["session_id"]).update(
            expires_at=past, created_at=past - timedelta(seconds=1)
        )
    elif state == "inactive":
        User.objects.filter(pk=nurse.user_id).update(is_active=False)
    elif state == "refresh":
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['refresh_token']}")
    else:
        historical = Token.objects.create(user=nurse.user)
        client.credentials(HTTP_AUTHORIZATION=f"Token {historical.key}")
        assert client.get("/api/v1/patient/?fields=id").status_code == 200
    assert_error(client.get(URL), "authentication_required")


def test_missing_nurse_is_read_only(user):
    pair = login(user.username, PASSWORD)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
    for method in ("get", "head", "options"):
        assert getattr(client, method)(URL).status_code == 200
    assert not Nurse.objects.filter(user=user).exists()
    response = client.get(URL)
    assert response.json() == {
        "count": 0,
        "page": 1,
        "page_size": 20,
        "next_page": None,
        "previous_page": None,
        "results": [],
    }
    assert_error(client.get(URL + "?page=2"), "page_not_found")


def test_isolation_pagination_order_schema_and_no_storage(read_context):
    client, nurse, _ = read_context
    other = Nurse.objects.create(
        user=User.objects.create_user(username="other@example.invalid")
    )
    patient(other, city="Foreign")
    rows = [patient(nurse, city=f"City {i}") for i in range(45)]
    nurse.user.is_staff = nurse.user.is_superuser = True
    nurse.user.save(update_fields=["is_staff", "is_superuser"])
    with (
        patch(
            "nurse.utils.private_files.signed_file_url",
            side_effect=AssertionError("storage called"),
        ),
        patch(
            "django.core.files.storage.default_storage.url",
            side_effect=AssertionError("storage called"),
        ),
    ):
        responses = [client.get(URL + f"?page={i}") for i in (1, 2, 3)]
    ids = []
    for i, response in enumerate(responses, start=1):
        assert response.status_code == 200
        assert_private(response)
        data = response.json()
        assert set(data) == PAGE_KEYS
        assert data["count"] == 45 and data["page"] == i and data["page_size"] == 20
        assert data["previous_page"] == (i - 1 if i > 1 else None)
        assert data["next_page"] == (i + 1 if i < 3 else None)
        assert len(data["results"]) == (20 if i < 3 else 5)
        for item in data["results"]:
            assert set(item) == PATIENT_KEYS
            assert item["birthday"] is None
            ids.append(item["id"])
    assert ids == [row.pk for row in rows]
    assert_error(client.get(URL + "?page=4"), "page_not_found")
    assert_error(client.get(URL + "?page=2147483647"), "page_not_found")


def test_page_size_maximum_and_count(read_context):
    client, nurse, _ = read_context
    rows = Patient.objects.bulk_create(
        [Patient(firstname="Fictif", lastname="Exemple") for _ in range(101)]
    )
    nurse.patients.add(*rows)
    response = client.get(URL + "?page_size=100")
    assert response.status_code == 200
    assert response.data["count"] == 101
    assert len(response.data["results"]) == 100
    assert response.data["next_page"] == 2
    assert (
        client.get(URL + "?page_size=100&page=2").data["results"][0]["id"]
        == rows[-1].pk
    )
    assert client.get(URL + "?page_size=1").data["page_size"] == 1


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "page=-1",
        "page=+1",
        "page=01",
        "page=1.0",
        "page=%201",
        "page=1%20",
        "page=2147483648",
        "page=١",
        "page=",
        "page=" + "9" * 5000,
        "page_size=0",
        "page_size=101",
        "page_size=-1",
        "page_size=01",
        "page_size=1.1",
        "page_size=",
        "page=1&page=1",
        "page_size=20&page_size=20",
        "search=a&search=a",
        "search=&search=",
        "unknown=a",
        "fields=id",
        "ordering=id",
        "patient=1",
        "search=" + "é" * 101,
    ],
)
def test_query_strictness(read_context, query):
    client, _, _ = read_context
    assert_error(client.get(URL + "?" + query), "invalid_query")


@pytest.mark.parametrize(
    "search",
    [
        "ÉLODIE LYON",
        "elodie lyon",
        "e\u0301lodie\u2003LYON",
        "exemple 060",
        "strasse lyon",
    ],
)
def test_search_global_multi_terms_unicode(read_context, search):
    client, nurse, _ = read_context
    for i in range(25):
        patient(nurse, city=f"Other {i}")
    target = Patient.objects.create(
        firstname="Élodie", lastname="Straße Exemple", city="LYON", phone="0600000000"
    )
    nurse.patients.add(target)
    foreign = Nurse.objects.create(
        user=User.objects.create_user(username="foreign@example.invalid")
    )
    foreign.patients.add(
        Patient.objects.create(
            firstname="Élodie",
            lastname="Straße Exemple",
            city="LYON",
            phone="0600000000",
        )
    )
    assert target.pk not in [item["id"] for item in client.get(URL).data["results"]]
    response = client.get(URL, {"search": search})
    assert response.status_code == 200
    assert response.data["count"] == 1
    assert response.data["results"][0]["id"] == target.pk
    assert response.data["page"] == 1


@pytest.mark.parametrize("literal", ["%", "_", "*", "\\", "Æ", "œ"])
def test_search_literals_and_no_compatibility_decomposition(read_context, literal):
    client, nurse, _ = read_context
    target = patient(nurse, city=literal)
    patient(nurse, city="normal")
    result = client.get(URL, {"search": literal})
    assert result.status_code == 200
    assert [item["id"] for item in result.data["results"]] == [target.pk]
    if literal in ("Æ", "œ"):
        assert (
            client.get(URL, {"search": "ae" if literal == "Æ" else "oe"}).data["count"]
            == 0
        )


def test_search_empty_no_result_and_length_before_normalization(read_context):
    client, nurse, _ = read_context
    patient(nurse, city="Lyon")
    for search in ("", " \u2003\t\n "):
        assert client.get(URL, {"search": search}).data["count"] == 1
    assert client.get(URL, {"search": "Missing"}).data["results"] == []
    assert client.get(URL, {"search": "x" * 100}).status_code == 200
    assert_error(client.get(URL, {"search": "\u0301" * 101}), "invalid_query")
    assert client.get(URL, {"search": "\u0301" * 100}).data["count"] == 1


def test_sql_pagination_bounded_query_count(read_context):
    client, nurse, _ = read_context
    rows = Patient.objects.bulk_create(
        [Patient(firstname="Fictif", lastname="Élodie", city="Lyon") for _ in range(45)]
    )
    nurse.patients.add(*rows)
    with CaptureQueriesContext(connection) as captured:
        response = client.get(URL, {"search": "elodie lyon", "page": "2"})
    assert response.status_code == 200
    assert len(response.data["results"]) == 20
    assert len(captured) == 3  # M02 auth, SQL COUNT, SQL page. No per-row queries.
    sql = captured[-1]["sql"]
    assert "LIMIT 20 OFFSET 20" in sql
    assert "search_" in sql
    assert ("INSTR" if connection.vendor == "sqlite" else "STRPOS") in sql
    assert "photo_prescription" not in sql and "health_card_number" not in sql


def test_head_options_methods_cache_and_conditional_headers(read_context):
    client, nurse, _ = read_context
    patient(nurse)
    get = client.get(URL, HTTP_IF_NONE_MATCH='"synthetic"')
    head = client.head(URL)
    assert get.status_code == head.status_code == 200
    assert head.content == b""
    for response in (get, head, client.options(URL)):
        assert_private(response)
        assert response["Allow"] == "GET, HEAD, OPTIONS"
    assert client.options(URL).data == {"allowed_methods": ["GET", "HEAD", "OPTIONS"]}
    assert_error(client.options(URL + "?fields=id"), "invalid_query")
    for method in ("post", "put", "patch", "delete"):
        assert_error(getattr(client, method)(URL), "method_not_allowed")
    assert client.get(URL, HTTP_ACCEPT="text/html").status_code == 200


def test_managed_unavailability_and_throttle_headers(read_context):
    client, _, _ = read_context
    with patch.object(
        PatientReadList, "get", side_effect=OperationalError("Synthetic sensitive data")
    ):
        assert_error(client.get(URL), "service_unavailable")
    with patch.object(PatientReadList, "get", side_effect=Throttled(wait=1.2)):
        response = client.get(URL)
    assert_error(response, "rate_limited")
    assert response["Retry-After"] == "2"


def test_routes_openapi_and_closed_contract():
    assert reverse("v2:reads:patients-list") == URL
    assert resolve(URL).func.view_class is PatientReadList
    schema = SchemaGenerator().get_schema(request=None, public=True)
    operation = schema["paths"][URL]["get"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert {p["name"] for p in operation["parameters"]} == {
        "page",
        "page_size",
        "search",
    }
    schemas = schema["components"]["schemas"]
    assert set(schemas["PatientSummary"]["properties"]) == PATIENT_KEYS
    assert set(schemas["PatientPage"]["properties"]) == PAGE_KEYS
    assert set(schemas["ReadError"]["properties"]) == {"code", "message"}
    for name in ("PatientSummary", "PatientPage", "ReadError"):
        assert schemas[name]["additionalProperties"] is False
        assert set(schemas[name]["required"]) == set(schemas[name]["properties"])


def test_v1_array_fields_historical_and_bearer_and_m02_rotation(read_context):
    import uuid

    client, nurse, pair = read_context
    target = patient(nurse)
    before = client.get("/api/v1/patient/?fields=id,firstname")
    assert before.status_code == 200 and before.data == [
        {"id": target.pk, "firstname": target.firstname}
    ]
    rotated = rotate(pair["refresh_token"], uuid.uuid4())
    assert_error(client.get(URL), "authentication_required")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {rotated['access_token']}")
    assert client.get(URL).status_code == 200
    revoke(AuthSession.objects.get(pk=pair["session_id"]))
    assert_error(client.get(URL), "authentication_required")
    # These backend 401s make no claim about future mobile purge/navigation.
    historical = Token.objects.create(user=nurse.user)
    client.credentials(HTTP_AUTHORIZATION=f"Token {historical.key}")
    assert client.get("/api/v1/patient/?fields=id").data == [{"id": target.pk}]
    assert_error(client.get(URL), "authentication_required")


@pytest.mark.parametrize("debug", [True, False])
@pytest.mark.parametrize("method", ["get", "head"])
def test_unexpected_500_is_private_generic_json(read_context, caplog, debug, method):
    client, _, _ = read_context
    client.raise_request_exception = False
    marker = "SYNTHETIC_M03_UNEXPECTED_500"
    with (
        override_settings(DEBUG=debug),
        patch.object(PatientReadList, "get", side_effect=RuntimeError(marker)),
    ):
        response = getattr(client, method)(URL + "?search=" + marker)
    assert response.status_code == 500
    assert_private(response)
    if method == "head":
        assert response.content == b""
    else:
        assert response.json() == {"error": "Server Error (500)"}
    assert marker not in caplog.text
    assert marker not in response.content.decode()


def test_preflight_does_not_bypass_v2_auth_and_preserves_legacy_cors(read_context):
    client, _, _ = read_context
    headers = {
        "HTTP_ORIGIN": "https://synthetic.invalid",
        "HTTP_ACCESS_CONTROL_REQUEST_METHOD": "GET",
    }
    with override_settings(CORS_ALLOWED_ORIGINS=["https://synthetic.invalid"]):
        response = APIClient().options(URL, **headers)
        assert_error(response, "authentication_required")
        assert response["Access-Control-Allow-Origin"] == "https://synthetic.invalid"
        authenticated = client.options(URL, **headers)
        assert authenticated.status_code == 200
        assert authenticated["Access-Control-Allow-Methods"] == "GET, HEAD, OPTIONS"
        assert authenticated.data == {"allowed_methods": ["GET", "HEAD", "OPTIONS"]}
        assert (
            authenticated["Access-Control-Allow-Origin"] == "https://synthetic.invalid"
        )
        legacy = APIClient().options("/api/v1/patient/", **headers)
        assert legacy.status_code == 200
        assert legacy.content == b""
        assert "POST" in legacy["Access-Control-Allow-Methods"]
        assert legacy["Access-Control-Allow-Origin"] == "https://synthetic.invalid"


def test_query_field_limit_authenticates_before_parsing(read_context):
    client, _, _ = read_context
    with override_settings(DATA_UPLOAD_MAX_NUMBER_FIELDS=2):
        url = URL + "?page=1&page=1&page=1"
        assert_error(APIClient().get(url), "authentication_required")
        assert_error(client.get(url), "invalid_query")
