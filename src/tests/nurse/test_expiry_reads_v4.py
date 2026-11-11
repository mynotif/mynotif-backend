"""Public expiry v4 contract; fixtures use real B01 login on synthetic accounts."""

import base64
import hashlib
import re
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import BadRequest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection
from django.http import Http404
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from freezegun import freeze_time
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from main.models import AuthSession, AuthToken
from main.session_services import login
from nurse.models import (
    DocumentImport,
    DocumentImportState,
    Nurse,
    Patient,
    Prescription,
    PrescriptionDocumentAsset,
    PrescriptionDocumentVersion,
)
from tests.nurse.test_document_reads_v3 import PNG

pytestmark = pytest.mark.django_db
ROOT = "/api/v4/expiry/"
LIST = ROOT + "prescriptions/"
DAY = date(2026, 3, 28)
PASSWORD = "Synthetic-Expiry-280!"
PAGE_KEYS = {
    "contract_revision",
    "context",
    "filter",
    "ordering",
    "page_size",
    "count",
    "next_cursor",
    "results",
}
CONTEXT_KEYS = {
    "business_date",
    "timezone",
    "validated_at",
    "next_day_at",
    "revalidate_in_ms",
    "scope_revision",
    "collection_revision",
    "calculation_version",
    "classification_version",
    "mutation_epoch",
}
ITEM_KEYS = {
    "id",
    "patient",
    "prescribing_doctor",
    "start_date",
    "end_date",
    "revision",
    "date_state",
    "to_renew",
    "proximity",
    "followup_state",
    "replacement",
    "document_version",
}
DETAIL_KEYS = {
    "contract_revision",
    "context",
    "item",
    "can_correct_dates",
    "can_confirm_renewal",
}


def assert_private(response):
    assert response["Cache-Control"] == "private, no-store"
    assert response["Pragma"] == "no-cache"
    assert "ETag" not in response
    assert response["X-Content-Type-Options"] == "nosniff"


def assert_error(response, status, code):
    assert response.status_code == status
    assert_private(response)
    body = response.json()
    assert set(body) == {"code", "message", "fields"}
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["fields"], dict)
    assert all(
        isinstance(codes, list)
        and set(codes) <= {"required", "invalid", "unknown", "inconsistent", "not_true"}
        for codes in body["fields"].values()
    )


def make_prescription(patient, **fields):
    return Prescription.objects.create(
        **{
            "patient": patient,
            "prescribing_doctor": "Dr synthétique",
            "start_date": DAY - timedelta(days=20),
            "end_date": DAY + timedelta(days=5),
            "photo_prescription": "",
            **fields,
        }
    )


class ExpiryAccount:
    def __init__(self):
        self.user = User.objects.create_user(
            username=f"expiry-{uuid.uuid4()}@example.invalid",
            password=PASSWORD,
        )
        self.nurse = Nurse.objects.create(user=self.user)
        self.patient = Patient.objects.create(
            firstname="Prénom synthétique",
            lastname="Nom synthétique",
            phone="SECRET_PHONE_280",
            street="SECRET_ADDRESS_280",
        )
        self.nurse.patients.add(self.patient)
        self.pair = login(self.user.username, PASSWORD)
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.pair['access_token']}"
        )

    def prescription(self, **fields):
        return make_prescription(self.patient, **fields)

    def page(self, **query):
        response = self.client.get(LIST, query)
        assert response.status_code == 200
        return response.json()

    def detail(self, prescription, patient=None):
        patient = patient or self.patient
        return self.client.get(
            f"{ROOT}patients/{patient.pk}/prescriptions/{prescription.pk}/"
        )


@pytest.fixture
def expiry_settings(settings, tmp_path):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.MEDIA_ROOT = str(tmp_path)
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }


@pytest.fixture
def expiry_setup(expiry_settings):
    with freeze_time("2026-03-28T12:00:00Z"):
        account = ExpiryAccount()
        account.prescription()
        yield account


def make_document_version(account, prescription, version_number=1):
    imported = DocumentImport.objects.create(
        owner=account.user,
        patient=account.patient,
        import_uuid=uuid.uuid4(),
        state=DocumentImportState.CONFIRMED,
        prescription=prescription,
        expires_at=timezone.now() + timedelta(days=1),
        metadata={
            "pages": [{"asset_index": 0, "source_page": 1, "rotation": 0, "crop": None}]
        },
    )
    version = PrescriptionDocumentVersion.objects.create(
        prescription=prescription,
        document_import=imported,
        version=version_number,
    )
    asset = PrescriptionDocumentAsset.objects.create(
        document_import=imported,
        document_version=version,
        role="original",
        original_index=0,
        storage_key=f"synthetic/missing-{uuid.uuid4()}.png",
        mime_type="image/png",
        byte_size=len(PNG),
        page_count=1,
        sha256=hashlib.sha256(PNG).hexdigest(),
    )
    return imported, version, asset


def test_list_and_detail_are_closed_preserve_sources_and_disable_mutations(
    expiry_setup,
):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    prescription.start_date, prescription.end_date = date(2027, 1, 1), date(2020, 2, 29)
    prescription.save(update_fields=["start_date", "end_date"])
    response = h.client.get(LIST)
    assert response.status_code == 200
    assert_private(response)
    page = response.json()
    assert set(page) == PAGE_KEYS
    assert page["contract_revision"] == "0.2-decision"
    assert (page["filter"], page["ordering"], page["page_size"]) == (
        "all",
        "end_date_asc",
        20,
    )
    assert page["count"] == 1 and page["next_cursor"] is None
    assert set(page["context"]) == CONTEXT_KEYS
    for key in CONTEXT_KEYS - {
        "business_date",
        "timezone",
        "validated_at",
        "next_day_at",
        "revalidate_in_ms",
    }:
        assert isinstance(page["context"][key], str)
        assert 1 <= len(page["context"][key]) <= 128
    item = page["results"][0]
    assert set(item) == ITEM_KEYS
    assert item["patient"] == {
        "id": h.patient.pk,
        "firstname": h.patient.firstname,
        "lastname": h.patient.lastname,
    }
    assert (item["start_date"], item["end_date"]) == ("2027-01-01", "2020-02-29")
    assert (item["date_state"], item["to_renew"], item["proximity"]) == (
        "invalid",
        False,
        "none",
    )
    assert item["followup_state"] == "to_process" and item["replacement"] is None
    assert item["document_version"] is None
    detail = h.detail(prescription)
    assert_private(detail)
    assert detail.status_code == 200 and set(detail.json()) == DETAIL_KEYS
    assert detail.json()["item"] == item
    assert detail.json()["can_correct_dates"] is False
    assert detail.json()["can_confirm_renewal"] is False
    assert "SECRET_" not in response.content.decode()


@pytest.mark.parametrize("ordering", ["end_date_asc", "end_date_desc"])
def test_total_order_nulls_last_and_account_wide_pagination(expiry_setup, ordering):
    h = expiry_setup
    Prescription.objects.filter(patient=h.patient).delete()
    other_patient = Patient.objects.create(firstname="Autre", lastname="Associé")
    h.nurse.patients.add(other_patient)
    rows = [
        make_prescription(other_patient, end_date=DAY + timedelta(days=2)),
        h.prescription(end_date=DAY),
        h.prescription(end_date=DAY),
        h.prescription(end_date=None),
    ]
    ordered = [rows[1].pk, rows[2].pk, rows[0].pk]
    if ordering == "end_date_desc":
        ordered.reverse()
    ordered.append(rows[3].pk)
    page = h.page(ordering=ordering, page_size=1)
    ids = []
    while True:
        assert page["count"] == 4 and len(page["results"]) == 1
        ids.extend(item["id"] for item in page["results"])
        if page["next_cursor"] is None:
            break
        assert len(ids) <= 4
        page = h.page(ordering=ordering, page_size=1, cursor=page["next_cursor"])
    assert ids == ordered


@pytest.mark.parametrize(
    "filter_name,expected",
    [
        ("all", 7),
        ("to_renew", 2),
        ("expired", 1),
        ("unknown", 1),
        ("future", 1),
        ("invalid", 1),
        ("renewed", 0),
    ],
)
def test_filter_runs_before_count_and_page_on_history_and_imports(
    expiry_setup, filter_name, expected
):
    h = expiry_setup
    h.prescription(end_date=DAY)
    h.prescription(end_date=DAY - timedelta(days=1))
    h.prescription(start_date=None)
    h.prescription(start_date=DAY + timedelta(days=1))
    h.prescription(start_date=DAY + timedelta(days=2), end_date=DAY)
    imported = h.prescription(end_date=DAY + timedelta(days=40))
    make_document_version(h, imported)
    page = h.page(filter=filter_name, page_size=1)
    assert page["count"] == expected
    assert len(page["results"]) == min(expected, 1)
    assert (page["next_cursor"] is not None) is (expected > 1)
    if filter_name == "to_renew":
        assert all(item["to_renew"] for item in page["results"])
    assert h.page(page_size=100)["count"] == 7


@pytest.mark.parametrize(
    "query",
    [
        "account=123",
        "page=1",
        "filter=near",
        "filter=ALL",
        "ordering=id",
        "page_size=0",
        "page_size=101",
        "page_size=-1",
        "page_size=1.0",
        "page_size=true",
        "page_size=01",
        "page_size=",
        "filter=all&filter=all",
        "ordering=end_date_asc&ordering=end_date_asc",
        "page_size=1&page_size=1",
        "cursor=x&cursor=x",
    ],
)
def test_closed_query_rejects_unknown_duplicate_or_coercible_values(
    expiry_setup, query
):
    assert_error(expiry_setup.client.get(LIST + "?" + query), 400, "invalid_request")


def test_malformed_and_other_account_cursor_are_nonrevealing(expiry_setup):
    h = expiry_setup
    h.prescription()
    cursor = h.page(page_size=1)["next_cursor"]
    assert isinstance(cursor, str) and 1 <= len(cursor) <= 2048
    readable = cursor.encode()
    try:
        readable += base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    except ValueError:
        pass
    assert h.patient.firstname.encode() not in readable
    assert h.user.username.encode() not in readable
    assert_error(h.client.get(LIST, {"cursor": "not-a-cursor"}), 400, "invalid_cursor")
    other = ExpiryAccount()
    assert_error(
        other.client.get(LIST, {"page_size": 1, "cursor": cursor}),
        400,
        "invalid_cursor",
    )


@pytest.mark.parametrize(
    "changed_query",
    [{"filter": "expired"}, {"ordering": "end_date_desc"}, {"page_size": 2}],
)
def test_cursor_is_bound_to_query_context(expiry_setup, changed_query):
    h = expiry_setup
    h.prescription()
    cursor = h.page(page_size=1)["next_cursor"]
    assert_error(
        h.client.get(LIST, {"page_size": 1, "cursor": cursor, **changed_query}),
        409,
        "pagination_invalidated",
    )


def test_bearer_and_current_association_only_no_staff_bypass(expiry_setup):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    outsider = ExpiryAccount()
    outsider.user.is_staff = outsider.user.is_superuser = True
    outsider.user.save(update_fields=["is_staff", "is_superuser"])
    assert outsider.page()["count"] == 0
    assert_error(outsider.detail(prescription, h.patient), 404, "not_found")
    assert_error(h.detail(prescription, outsider.patient), 404, "not_found")
    legacy = APIClient()
    token = Token.objects.create(user=h.user)
    legacy.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    assert_error(legacy.get(LIST), 401, "authentication_required")
    assert_error(APIClient().get(LIST), 401, "authentication_required")
    h.nurse.patients.remove(h.patient)
    assert h.page()["count"] == 0
    assert_error(h.detail(prescription), 404, "not_found")


@pytest.mark.parametrize(
    "failure",
    [
        "access_expired",
        "access_revoked",
        "refresh",
        "session_expired",
        "session_revoked",
        "inactive",
        "fingerprint",
        "password",
        "secret",
        "missing_part",
        "malformed_token",
        "basic",
        "non_ascii",
    ],
)
def test_invalid_b01_identity_is_read_only_closed_401(expiry_setup, failure):
    h = expiry_setup
    token_id, secret = h.pair["access_token"].split(".")
    access = AuthToken.objects.filter(pk=token_id)
    session = AuthSession.objects.filter(pk=h.pair["session_id"])
    user = User.objects.filter(pk=h.user.pk)
    now = timezone.now()
    changes = {
        "access_expired": lambda: access.update(
            issued_at=now - timedelta(hours=2), expires_at=now - timedelta(hours=1)
        ),
        "access_revoked": lambda: access.update(revoked_at=now),
        "session_expired": lambda: session.update(
            created_at=now - timedelta(days=2), expires_at=now - timedelta(days=1)
        ),
        "session_revoked": lambda: session.update(revoked_at=now),
        "inactive": lambda: user.update(is_active=False),
        "fingerprint": lambda: session.update(password_fingerprint="x" * 64),
        "password": lambda: user.update(password="synthetic-changed"),
    }
    changes.get(failure, lambda: None)()
    wrong_secret = ("A" if secret[0] != "A" else "B") + secret[1:]
    header = {
        "refresh": "Bearer " + h.pair["refresh_token"],
        "secret": f"Bearer {token_id}.{wrong_secret}",
        "missing_part": "Bearer",
        "malformed_token": "Bearer invalid",
        "basic": "Basic " + h.pair["access_token"],
        "non_ascii": b"Bearer \xff",
    }.get(failure, "Bearer " + h.pair["access_token"])
    h.client.credentials(HTTP_AUTHORIZATION=header)
    models = (AuthSession, AuthToken)
    before = [list(model.objects.order_by("pk").values()) for model in models]
    with CaptureQueriesContext(connection) as queries:
        response = h.client.get(LIST)
    assert_error(response, 401, "authentication_required")
    assert response["WWW-Authenticate"] == "Bearer"
    assert before == [list(model.objects.order_by("pk").values()) for model in models]
    assert not any(
        query["sql"].lstrip().split()[0].upper()
        in {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "CREATE", "ALTER"}
        for query in queries
    )


@pytest.mark.parametrize(
    "instant,business_date,next_day_at,milliseconds",
    [
        ("2026-03-28T23:00:00Z", "2026-03-29", "2026-03-29T22:00:00Z", 23 * 3600000),
        ("2026-10-24T22:00:00Z", "2026-10-25", "2026-10-25T23:00:00Z", 25 * 3600000),
        ("2026-03-28T22:59:59Z", "2026-03-28", "2026-03-28T23:00:00Z", 1000),
        (
            "2026-03-28T23:00:01Z",
            "2026-03-29",
            "2026-03-29T22:00:00Z",
            23 * 3600000 - 1000,
        ),
    ],
)
def test_context_uses_paris_midnight_including_dst(
    expiry_settings, instant, business_date, next_day_at, milliseconds
):
    with freeze_time(instant):
        h = ExpiryAccount()
        context = h.page()["context"]
    assert set(context) == CONTEXT_KEYS
    assert context["business_date"] == business_date
    assert context["timezone"] == "Europe/Paris"
    assert context["next_day_at"] == next_day_at
    assert type(context["revalidate_in_ms"]) is int
    assert context["revalidate_in_ms"] == milliseconds
    assert context["validated_at"].endswith("Z")
    assert datetime.fromisoformat(context["validated_at"]) == datetime.fromisoformat(
        instant
    )


def test_midnight_changes_context_and_invalidates_prior_cursor(expiry_settings):
    with freeze_time("2026-03-28T22:59:59Z") as clock:
        h = ExpiryAccount()
        h.prescription(end_date=DAY)
        h.prescription()
        before = h.page(page_size=1)
        assert before["results"][0]["date_state"] == "valid"
        clock.tick(delta=timedelta(seconds=2))
        assert_error(
            h.client.get(LIST, {"page_size": 1, "cursor": before["next_cursor"]}),
            409,
            "pagination_invalidated",
        )
        after = h.page(page_size=1)
    assert after["results"][0]["date_state"] == "expired"
    assert (
        after["context"]["collection_revision"]
        != before["context"]["collection_revision"]
    )


@pytest.mark.parametrize("change", ["midnight", "session_revocation"])
def test_context_changes_during_clinical_select_close_the_response(
    expiry_settings, change
):
    with freeze_time("2026-03-28T22:59:59Z") as clock:
        h = ExpiryAccount()
        h.prescription()
        observed = []
        actions = {
            "midnight": lambda: clock.tick(delta=timedelta(seconds=2)),
            "session_revocation": lambda: AuthSession.objects.filter(
                pk=h.pair["session_id"]
            ).update(revoked_at=timezone.now()),
        }

        def change_after_clinical_select(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if not observed and re.search(
                r'^\s*SELECT\b.*\bFROM\s+"?nurse_prescription\b', sql, re.I | re.S
            ):
                observed.append(sql)
                actions[change]()
            return result

        with connection.execute_wrapper(change_after_clinical_select):
            response = h.client.get(LIST)
        assert len(observed) == 1, "The change must land during a clinical SELECT"
        status, code = {
            "midnight": (409, "pagination_invalidated"),
            "session_revocation": (401, "authentication_required"),
        }[change]
        assert_error(response, status, code)


@pytest.mark.parametrize("version", ["CALCULATION_VERSION", "CLASSIFICATION_VERSION"])
def test_rule_version_change_invalidates_previous_cursor(expiry_setup, version):
    h = expiry_setup
    h.prescription()
    before = h.page(page_size=1)
    with patch("nurse.expiry_state." + version, "synthetic-new-rule"):
        assert_error(
            h.client.get(LIST, {"page_size": 1, "cursor": before["next_cursor"]}),
            409,
            "pagination_invalidated",
        )
        current = h.page()
    assert (
        current["context"]["collection_revision"]
        != before["context"]["collection_revision"]
    )


def test_missing_source_is_null_and_list_does_not_read_document_bytes(expiry_setup):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    make_document_version(h, prescription)
    # A name or metadata pointing at absent bytes cannot prove availability.
    with patch(
        "django.core.files.storage.default_storage.open",
        side_effect=AssertionError("bytes"),
    ):
        page = h.page()
    assert page["results"][0]["document_version"] is None
    assert h.detail(prescription).json()["item"]["document_version"] is None
    assert "storage_key" not in str(page) and "synthetic/missing" not in str(page)


def test_available_import_source_reports_its_current_document_version(expiry_setup):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    _, _, asset = make_document_version(h, prescription)
    default_storage.save(asset.storage_key, ContentFile(PNG))
    with patch.object(default_storage, "open", side_effect=AssertionError("bytes")):
        assert h.detail(prescription).json()["item"]["document_version"] == 1
        assert h.page()["results"][0]["document_version"] == 1


@pytest.mark.parametrize("available", [False, True])
def test_legacy_source_version_requires_present_file_without_open(
    expiry_setup, available
):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    filename = "synthetic-legacy.png"
    Prescription.objects.filter(pk=prescription.pk).update(photo_prescription=filename)
    if available:
        default_storage.save(filename, ContentFile(PNG))
    with patch.object(default_storage, "open", side_effect=AssertionError("bytes")):
        expected = 1 if available else None
        assert h.page()["results"][0]["document_version"] == expected
        assert h.detail(prescription).json()["item"]["document_version"] == expected


def test_cors_preflight_is_authenticated_and_preserves_private_security_headers(
    expiry_setup, settings
):
    settings.CORS_ALLOWED_ORIGINS = ["https://expiry.invalid"]
    headers = {
        "HTTP_ORIGIN": "https://expiry.invalid",
        "HTTP_ACCESS_CONTROL_REQUEST_METHOD": "GET",
    }
    unauthorized = APIClient().options(LIST, **headers)
    assert_error(unauthorized, 401, "authentication_required")
    assert unauthorized["Access-Control-Allow-Origin"] == "https://expiry.invalid"
    authenticated = expiry_setup.client.options(LIST, **headers)
    assert authenticated.status_code == 200
    assert_private(authenticated)
    assert authenticated["Access-Control-Allow-Methods"] == "GET, HEAD, OPTIONS"
    assert authenticated["Access-Control-Allow-Origin"] == "https://expiry.invalid"


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize(
    "status,exception,code",
    [
        (400, BadRequest, "invalid_request"),
        (404, Http404, "not_found"),
        (500, RuntimeError, "internal_error"),
    ],
)
def test_django_errors_keep_closed_body_and_cors_allowlist(
    expiry_setup, settings, allowed, status, exception, code
):
    settings.CORS_ALLOWED_ORIGINS = ["https://expiry.invalid"]
    origin = "https://expiry.invalid" if allowed else "https://untrusted.invalid"
    h = expiry_setup
    with (
        patch(
            "nurse.expiry_views.ExpiryPrescriptionList.dispatch",
            side_effect=exception("SECRET_CLINICAL"),
        ),
        patch.object(h.client, "raise_request_exception", False),
    ):
        response = h.client.get(LIST, HTTP_ORIGIN=origin)
    assert_error(response, status, code)
    assert "SECRET_CLINICAL" not in response.content.decode()
    if allowed:
        assert response["Access-Control-Allow-Origin"] == origin
    else:
        assert "Access-Control-Allow-Origin" not in response


def test_get_does_not_write_clinical_data_or_create_read_authority(expiry_setup):
    h = expiry_setup
    prescription = Prescription.objects.get(patient=h.patient)
    with CaptureQueriesContext(connection) as queries:
        h.page()
        assert h.detail(prescription).status_code == 200
    writes = [
        q["sql"]
        for q in queries
        if q["sql"].lstrip().split()[0].upper()
        in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "REPLACE",
            "CREATE",
            "ALTER",
        }
    ]
    assert writes == []


def test_mutations_remain_unavailable_and_openapi_exposes_only_reads(expiry_setup):
    h = expiry_setup
    assert_error(h.client.post(LIST, {}, format="json"), 405, "method_not_allowed")
    for path in [ROOT + "commands/", ROOT + "reconciliation-fence/"]:
        assert h.client.post(path, {}, format="json").status_code == 404
    schema = SchemaGenerator().get_schema(request=None, public=True)
    paths = {
        path: value for path, value in schema["paths"].items() if path.startswith(ROOT)
    }
    assert set(paths) == {
        LIST,
        ROOT + "patients/{patient_id}/prescriptions/{prescription_id}/",
    }
    assert all(set(operations) == {"get"} for operations in paths.values())
    operation = paths[LIST]["get"]
    assert {p["name"] for p in operation["parameters"]} == {
        "filter",
        "ordering",
        "page_size",
        "cursor",
    }
    response_schema = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    while "$ref" in response_schema:
        response_schema = schema["components"]["schemas"][
            response_schema["$ref"].split("/")[-1]
        ]
    assert set(response_schema["properties"]) == PAGE_KEYS
    assert set(response_schema["required"]) == PAGE_KEYS
    assert response_schema["additionalProperties"] is False
