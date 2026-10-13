from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from main.models import AuthSession, AuthToken
from main.session_services import login, revoke
from nurse.models import Nurse, Patient, Prescription
from nurse.read_prescription_views import PrescriptionReadDetail, PrescriptionReadList
from tests.conftest import PASSWORD
from tests.nurse.test_read_patients_v2 import assert_error, assert_private

pytestmark = pytest.mark.django_db
KEYS = {"id", "patient_id", "prescribing_doctor", "start_date", "end_date"}


@pytest.fixture
def rx(user):
    pair = login(user.username, PASSWORD)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
    nurse = Nurse.objects.create(user=user)
    patients = [
        Patient.objects.create(firstname="Fictif", lastname=f"Exemple {i}")
        for i in range(3)
    ]
    nurse.patients.add(*patients[:2])
    other = Nurse.objects.create(
        user=User.objects.create_user(username="other-rx@example.invalid")
    )
    other.patients.add(patients[2])
    prescriptions = [make_prescription(item) for item in patients]
    return client, nurse, patients, prescriptions, pair


def make_prescription(patient, **fields):
    data = dict(
        patient=patient,
        prescribing_doctor="Prescripteur fictif",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        email_doctor="fictif@example.invalid",
        photo_prescription="synthetic-never-read.png",
    )
    return Prescription.objects.create(**{**data, **fields})


def collection(patient_id):
    return f"/api/v2/patients/{patient_id}/prescriptions/"


def detail(patient_id, prescription_id):
    return collection(patient_id) + f"{prescription_id}/"


def test_summary_list_detail_exact_source_and_no_storage(rx):
    client, _, patients, prescriptions, _ = rx
    obj = prescriptions[0]
    # Contract explicitly preserves dates, including clinically unusual sources.
    obj.start_date, obj.end_date = date(2027, 1, 1), date(2020, 2, 29)
    obj.save(update_fields=["start_date", "end_date"])
    with (
        patch(
            "nurse.utils.private_files.signed_file_url",
            side_effect=AssertionError("storage called"),
        ),
        patch(
            "nurse.utils.private_files.file_exists",
            side_effect=AssertionError("storage called"),
        ),
        patch(
            "django.core.files.storage.default_storage.url",
            side_effect=AssertionError("storage called"),
        ),
        patch("boto3.client", side_effect=AssertionError("S3 called")),
    ):
        listed = client.get(collection(patients[0].pk))
        fetched = client.get(detail(patients[0].pk, obj.pk))
    assert listed.status_code == fetched.status_code == 200
    for response in (listed, fetched):
        assert_private(response)
    expected = {
        "id": obj.pk,
        "patient_id": patients[0].pk,
        "prescribing_doctor": "Prescripteur fictif",
        "start_date": "2027-01-01",
        "end_date": "2020-02-29",
    }
    assert fetched.json() == expected
    assert listed.data["results"] == [expected]
    assert set(fetched.json()) == KEYS


@pytest.mark.parametrize("method", ["get", "head", "options"])
def test_rights_patient_prescription_and_identical_foreign_errors(rx, method):
    client, nurse, patients, prescriptions, _ = rx
    nurse.user.is_staff = nurse.user.is_superuser = True
    nurse.user.save(update_fields=["is_staff", "is_superuser"])
    foreign = patients[2]
    urls = [
        collection(foreign.pk),
        collection(9223372036854775807),
        detail(foreign.pk, prescriptions[2].pk),
        detail(patients[0].pk, prescriptions[1].pk),
        detail(patients[0].pk, prescriptions[2].pk),
        detail(patients[0].pk, 9223372036854775807),
    ]
    for url in urls:
        response = getattr(client, method)(url + "?unknown=sensitive")
        if method == "head":
            assert response.status_code == 404 and response.content == b""
            assert_private(response)
        else:
            assert_error(response, "not_found")
    assert (
        getattr(client, method)(detail(patients[0].pk, prescriptions[0].pk)).status_code
        == 200
    )


def test_explicit_shared_patient_is_authorized(rx):
    client, nurse, patients, prescriptions, _ = rx
    assert_error(client.get(collection(patients[2].pk)), "not_found")
    nurse.patients.add(patients[2])
    assert client.get(detail(patients[2].pk, prescriptions[2].pk)).status_code == 200
    nurse.patients.remove(patients[2])
    assert_error(client.get(detail(patients[2].pk, prescriptions[2].pk)), "not_found")


@pytest.mark.parametrize(
    "identifier",
    ["", "0", "-1", "+1", "a", "1.0", "١", "9223372036854775808", "9" * 100],
)
def test_malformed_path_auth_first_then_generic_not_found(rx, identifier):
    client, _, patients, prescriptions, _ = rx
    for url in (
        collection(identifier),
        detail(identifier, prescriptions[0].pk),
        detail(patients[0].pk, identifier),
    ):
        assert_error(APIClient().get(url + "?unknown=x"), "authentication_required")
        assert_error(client.get(url + "?unknown=x"), "not_found")


def test_prescription_pagination_stable_empty_and_count(rx):
    client, _, patients, prescriptions, _ = rx
    target = patients[0]
    for _ in range(44):
        make_prescription(target)
    ids = list(
        Prescription.objects.filter(patient=target)
        .order_by("-end_date", "-id")
        .values_list("id", flat=True)
    )
    fetched = []
    for page in (1, 2, 3):
        response = client.get(collection(target.pk), {"page": str(page)})
        assert response.status_code == 200
        assert response.data["count"] == 45
        assert response.data["page"] == page and response.data["page_size"] == 20
        assert response.data["previous_page"] == (page - 1 if page > 1 else None)
        assert response.data["next_page"] == (page + 1 if page < 3 else None)
        fetched.extend(item["id"] for item in response.data["results"])
    assert fetched == ids
    assert_error(client.get(collection(target.pk), {"page": "4"}), "page_not_found")
    for _ in range(56):
        make_prescription(target)
    assert (
        len(client.get(collection(target.pk), {"page_size": "100"}).data["results"])
        == 100
    )
    assert client.get(collection(target.pk), {"page_size": "100"}).data["count"] == 101
    assert (
        len(
            client.get(collection(target.pk), {"page_size": "100", "page": "2"}).data[
                "results"
            ]
        )
        == 1
    )
    Prescription.objects.filter(patient=patients[1]).delete()
    empty = client.get(collection(patients[1].pk))
    assert empty.status_code == 200
    assert empty.data == {
        "count": 0,
        "page": 1,
        "page_size": 20,
        "next_page": None,
        "previous_page": None,
        "results": [],
    }
    assert_error(
        client.get(collection(patients[1].pk), {"page": "2"}), "page_not_found"
    )


def test_prescription_date_order_and_id_tiebreaker(rx):
    client, _, patients, prescriptions, _ = rx
    older = make_prescription(patients[0], end_date=date(2020, 1, 1))
    newer = make_prescription(patients[0], end_date=date(2027, 1, 1))
    equal = make_prescription(patients[0])
    assert [
        item["id"] for item in client.get(collection(patients[0].pk)).data["results"]
    ] == [newer.pk, equal.pk, prescriptions[0].pk, older.pk]


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "page=01",
        "page=2147483648",
        "page_size=101",
        "page_size=0",
        "page=1&page=2",
        "page_size=1&page_size=2",
        "fields=id",
        "ordering=id",
        "patient=1",
        "search=anything",
        "search=x&search=x",
        "unknown=x",
    ],
)
@pytest.mark.parametrize("method", ["get", "options"])
def test_prescription_list_invalid_queries(rx, query, method):
    client, _, patients, _, _ = rx
    assert_error(
        getattr(client, method)(collection(patients[0].pk) + "?" + query),
        "invalid_query",
    )


@pytest.mark.parametrize(
    "query",
    [
        "page=1",
        "page_size=20",
        "search=",
        "fields=id",
        "unknown=x",
        "search=x&search=y",
    ],
)
@pytest.mark.parametrize("method", ["get", "options"])
def test_prescription_detail_rejects_all_queries(rx, query, method):
    client, _, patients, prescriptions, _ = rx
    assert_error(
        getattr(client, method)(
            detail(patients[0].pk, prescriptions[0].pk) + "?" + query
        ),
        "invalid_query",
    )


@pytest.mark.parametrize(
    "state", ["absent", "invalid", "expired", "revoked", "historical"]
)
def test_prescription_auth_and_read_without_nurse(rx, state):
    client, nurse, patients, prescriptions, pair = rx
    if state == "absent":
        client.credentials()
    elif state == "invalid":
        client.credentials(HTTP_AUTHORIZATION="Bearer invalid")
    elif state == "expired":
        past = timezone.now() - timedelta(seconds=1)
        AuthToken.objects.filter(pk=pair["access_token"].split(".")[0]).update(
            issued_at=past - timedelta(seconds=1), expires_at=past
        )
    elif state == "revoked":
        revoke(AuthSession.objects.get(pk=pair["session_id"]))
    else:
        token = Token.objects.create(user=nurse.user)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    for url in (
        collection(patients[0].pk),
        detail(patients[0].pk, prescriptions[0].pk),
    ):
        assert_error(client.get(url), "authentication_required")


def test_no_nurse_is_created(rx):
    client, nurse, patients, prescriptions, _ = rx
    nurse.delete()
    for url in (
        collection(patients[0].pk),
        detail(patients[0].pk, prescriptions[0].pk),
    ):
        for method in ("get", "head", "options"):
            response = getattr(client, method)(url)
            assert response.status_code == 404
            assert_private(response)
    assert Nurse.objects.count() == 1  # Only the other account's existing Nurse.


def test_head_options_mutations_cache_and_no_data(rx):
    client, _, patients, prescriptions, _ = rx
    for url in (
        collection(patients[0].pk),
        detail(patients[0].pk, prescriptions[0].pk),
    ):
        head = client.head(url)
        assert head.status_code == 200 and head.content == b""
        assert_private(head)
        options = client.options(url)
        assert options.status_code == 200
        assert options.data == {"allowed_methods": ["GET", "HEAD", "OPTIONS"]}
        assert options["Allow"] == "GET, HEAD, OPTIONS"
        assert_private(options)
        for method in ("post", "put", "patch", "delete"):
            assert_error(getattr(client, method)(url), "method_not_allowed")
        assert client.get(url, HTTP_IF_NONE_MATCH='"synthetic"').status_code == 200


def test_prescription_queries_are_bounded_no_file_columns(rx):
    client, _, patients, _, _ = rx
    for _ in range(22):
        make_prescription(patients[0])
    with CaptureQueriesContext(connection) as captured:
        response = client.get(collection(patients[0].pk), {"page": "2"})
    assert response.status_code == 200 and len(response.data["results"]) == 3
    assert len(captured) == 4  # M02, patient rights, COUNT, page.
    sql = captured[-1]["sql"]
    assert "LIMIT 20 OFFSET 20" in sql
    assert "photo_prescription" not in sql and "email_doctor" not in sql
    with CaptureQueriesContext(connection) as captured:
        response = client.get(detail(patients[0].pk, response.data["results"][0]["id"]))
    assert response.status_code == 200 and len(captured) == 3
    assert "photo_prescription" not in captured[-1]["sql"]


def test_prescription_routes_and_openapi_closed_schemas():
    assert reverse(
        "v2:reads:prescriptions-list", kwargs={"patient_id": 41}
    ) == collection(41)
    assert reverse(
        "v2:reads:prescriptions-detail",
        kwargs={"patient_id": 41, "prescription_id": 81},
    ) == detail(41, 81)
    assert resolve(collection(41)).func.view_class is PrescriptionReadList
    assert resolve(detail(41, 81)).func.view_class is PrescriptionReadDetail
    schema = SchemaGenerator().get_schema(request=None, public=True)
    paths = schema["paths"]
    for path in (
        "/api/v2/patients/{patient_id}/prescriptions/",
        "/api/v2/patients/{patient_id}/prescriptions/{prescription_id}/",
    ):
        assert set(paths[path]) == {"get"}
        assert paths[path]["get"]["security"] == [{"BearerAuth": []}]
    params = paths["/api/v2/patients/{patient_id}/prescriptions/{prescription_id}/"][
        "get"
    ]["parameters"]
    assert {param["name"] for param in params} == {"patient_id", "prescription_id"}
    for name in ("PrescriptionSummary", "PrescriptionPage"):
        component = schema["components"]["schemas"][name]
        assert component["additionalProperties"] is False
        assert set(component["required"]) == set(component["properties"])
    assert (
        set(schema["components"]["schemas"]["PrescriptionSummary"]["properties"])
        == KEYS
    )
