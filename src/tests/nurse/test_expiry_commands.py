"""#281 AC1–6 / RM1–8: correction wire 0.2-decision, through real B01 HTTP.

Synthetic internal setup interfaces are listed in the local test-author handoff.
No future transport or renewal behavior is inferred from a successful correction.
"""

import base64
import json
import uuid
from datetime import date, timedelta

import pytest
from django.apps import apps
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from freezegun import freeze_time
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from main.models import AuthSession, AuthToken
from main.session_services import login
from nurse.models import Nurse, Prescription
from tests.nurse import test_expiry_reads_v4 as reads

pytestmark = pytest.mark.django_db
expiry_settings = reads.expiry_settings
COMMANDS = reads.ROOT + "commands/"
FENCE = reads.ROOT + "reconciliation-fence/"
NAMESPACE = "93a16389-9d8c-4a4e-8e7d-9dc3707de6ec"
KEYS = {
    "v1": {
        "encryption": base64.b64encode(bytes(range(32))).decode(),
        "hmac": base64.b64encode(bytes(range(32, 64))).decode(),
    }
}
COMMAND_KEYS = {
    "command_id",
    "mutation_epoch",
    "kind",
    "state",
    "recorded_at",
    "result",
    "error",
}
RESULT_KEYS = {
    "patient_id",
    "prescription_id",
    "revision",
    "replacement_prescription_id",
    "collection_revision",
}
JOURNAL_KEYS = {
    "contract_revision",
    "mutation_epoch",
    "journal_revision",
    "next_cursor",
    "results",
}


def command_model(name="ExpiryCommand"):
    try:
        return apps.get_model("nurse", name)
    except LookupError:
        pytest.fail(f"#281 requires the additive nurse.{name} model")


def open_runtime():
    authority = command_model("ExpiryCommandAuthority")
    return authority.objects.update_or_create(
        pk=1, defaults={"namespace": NAMESPACE, "accepting": True}
    )[0]


def new_client(account):
    pair = login(account.user.username, reads.PASSWORD)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access_token']}")
    return client, pair


@pytest.fixture
def command_settings(expiry_settings, settings):
    settings.EXPIRY_COMMANDS_ENABLED = True
    settings.EXPIRY_COMMAND_NAMESPACE = NAMESPACE
    settings.EXPIRY_COMMAND_ACTIVE_KEY = "v1"
    settings.EXPIRY_COMMAND_KEYS = KEYS
    return settings


@pytest.fixture
def command_setup(command_settings):
    # Models are looked up in the test call, so missing product code is an
    # explicit red assertion rather than an import/collection/fixture error.
    def create():
        open_runtime()
        account = reads.ExpiryAccount()
        prescription = account.prescription()
        return account, prescription

    with freeze_time("2026-03-28T12:00:00Z"):
        yield create


def correction(account, prescription, **changes):
    response = account.detail(prescription)
    assert response.status_code == 200
    body = response.json()
    return {
        "mutation_epoch": body["context"]["mutation_epoch"],
        "patient_id": account.patient.pk,
        "prescription_id": prescription.pk,
        "expected_revision": body["item"]["revision"],
        "start_date": "2026-03-01",
        "end_date": "2026-04-01",
        **changes,
    }


def put_url(command_id):
    return f"{COMMANDS}{command_id}/date-correction/"


def assert_command(response, status, state):
    assert response.status_code == status, response.content
    reads.assert_private(response)
    body = response.json()
    assert set(body) == COMMAND_KEYS
    identifier = uuid.UUID(body["command_id"])
    assert identifier.version == 4 and str(identifier) == body["command_id"]
    assert body["kind"] == "date_correction" and body["state"] == state
    assert isinstance(body["mutation_epoch"], str) and body["mutation_epoch"]
    assert body["recorded_at"].endswith("Z")
    if state == "succeeded":
        assert set(body["result"]) == RESULT_KEYS
        assert body["result"]["replacement_prescription_id"] is None
        assert body["error"] is None
    elif state == "rejected":
        assert body["result"] is None
        assert set(body["error"]) == {"code", "message", "fields"}
    else:
        assert body["result"] is body["error"] is None
    return body


def admit(account, intent, client=None):
    response = (client or account.client).post(
        COMMANDS, {"kind": "date_correction", "intent": intent}, format="json"
    )
    return assert_command(response, 201, "pending")


def execute(account, command, intent, client=None):
    return (client or account.client).put(
        put_url(command["command_id"]), intent, format="json"
    )


def assert_unchanged(prescription, before):
    prescription.refresh_from_db()
    assert (prescription.start_date, prescription.end_date) == before


def test_ac1_admission_is_immutable_server_allocated_and_has_no_effect(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    before = (rx.start_date, rx.end_date)
    revision = h.detail(rx).json()["item"]["revision"]
    pending = admit(h, intent)
    assert pending["mutation_epoch"] == intent["mutation_epoch"]
    assert_unchanged(rx, before)
    assert h.detail(rx).json()["item"]["revision"] == revision
    result = h.client.get(f"{COMMANDS}{pending['command_id']}/")
    assert assert_command(result, 200, "pending") == pending
    assert_unchanged(rx, before)
    reads.assert_error(
        h.client.post(
            COMMANDS,
            {
                "kind": "date_correction",
                "intent": intent,
                "command_id": str(uuid.uuid4()),
            },
            format="json",
        ),
        400,
        "invalid_request",
    )


def test_ac6_open_runtime_enables_only_correction_capability(command_setup):
    h, rx = command_setup()
    detail = h.detail(rx).json()
    assert detail["can_correct_dates"] is True
    assert detail["can_confirm_renewal"] is False
    intent = correction(h, rx)
    pending = admit(h, intent)
    response = h.client.put(
        f"{COMMANDS}{pending['command_id']}/renewal-confirmation/",
        intent,
        format="json",
    )
    reads.assert_error(response, 404, "not_found")
    assert_command(h.client.get(f"{COMMANDS}{pending['command_id']}/"), 200, "pending")


def test_ac1_unknown_put_is_lookup_only_under_current_or_old_epoch(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    model = command_model()
    count = model.objects.count()
    for epoch in (intent["mutation_epoch"], "obsolete-epoch"):
        reads.assert_error(
            h.client.put(
                put_url(uuid.uuid4()),
                {**intent, "mutation_epoch": epoch},
                format="json",
            ),
            404,
            "not_found",
        )
    assert model.objects.count() == count


def test_rm4_correction_and_replay_have_one_effect_and_minimal_result(command_setup):
    h, rx = command_setup()
    _, document, _ = reads.make_document_version(h, rx)
    document_before = type(document).objects.filter(pk=document.pk).values().get()
    clinical_before = Prescription.objects.filter(pk=rx.pk).values().get()
    intent = correction(h, rx)
    before = h.page()["context"]["collection_revision"]
    pending = admit(h, intent)
    result = assert_command(execute(h, pending, intent), 200, "succeeded")
    rx.refresh_from_db()
    assert (rx.start_date, rx.end_date) == (date(2026, 3, 1), date(2026, 4, 1))
    clinical_after = Prescription.objects.filter(pk=rx.pk).values().get()
    for field in clinical_before.keys() - {"start_date", "end_date"}:
        assert clinical_after[field] == clinical_before[field]
    assert (
        type(document).objects.filter(pk=document.pk).values().get() == document_before
    )
    detail = h.detail(rx).json()
    assert detail["item"]["revision"] != intent["expected_revision"]
    assert result["result"] == {
        "patient_id": h.patient.pk,
        "prescription_id": rx.pk,
        "revision": detail["item"]["revision"],
        "replacement_prescription_id": None,
        "collection_revision": detail["context"]["collection_revision"],
    }
    assert result["result"]["collection_revision"] != before
    assert assert_command(execute(h, pending, intent), 200, "succeeded") == result
    assert h.detail(rx).json()["item"]["revision"] == result["result"]["revision"]
    assert "2026-04-01" not in json.dumps(result)


@pytest.mark.parametrize(
    "dates", [(None, None), (None, "2026-04-01"), ("2024-02-29", None)]
)
def test_rm1_null_dates_and_valid_leap_day_are_never_inferred(command_setup, dates):
    h, rx = command_setup()
    intent = correction(h, rx, start_date=dates[0], end_date=dates[1])
    assert_command(execute(h, admit(h, intent), intent), 200, "succeeded")
    current = h.detail(rx).json()["item"]
    assert (current["start_date"], current["end_date"]) == dates
    assert current["date_state"] == "unknown"
    assert current["to_renew"] is False


def test_rm4_identical_dates_are_two_distinct_events_with_new_revisions(command_setup):
    h, rx = command_setup()
    results = []
    for _ in range(2):
        intent = correction(h, rx)
        result = assert_command(execute(h, admit(h, intent), intent), 200, "succeeded")
        assert result["result"]["revision"] != intent["expected_revision"]
        results.append(result)
    assert results[0]["command_id"] != results[1]["command_id"]
    assert results[0]["result"]["revision"] != results[1]["result"]["revision"]
    journal = h.client.get(COMMANDS).json()
    assert {row["command_id"] for row in journal["results"]} == {
        row["command_id"] for row in results
    }


@pytest.mark.parametrize(
    "case,code", [("stale", "revision_conflict"), ("inverted", "invalid_dates")]
)
def test_ac2_business_rejection_is_durable_http200_command(command_setup, case, code):
    h, rx = command_setup()
    changes = {
        "stale": {"expected_revision": "obsolete-revision"},
        "inverted": {"start_date": "2027-01-01", "end_date": "2020-02-29"},
    }
    intent = correction(h, rx, **changes[case])
    before = (rx.start_date, rx.end_date)
    pending = admit(h, intent)
    result = assert_command(execute(h, pending, intent), 200, "rejected")
    assert result["error"]["code"] == code
    assert_unchanged(rx, before)
    assert assert_command(execute(h, pending, intent), 200, "rejected") == result


INVALID_FIELDS = [
    ("patient_id", "1"),
    ("patient_id", True),
    ("patient_id", 0),
    ("prescription_id", 1.0),
    ("prescription_id", 9007199254740992),
    ("expected_revision", ""),
    ("expected_revision", "r" * 129),
    ("mutation_epoch", None),
    ("start_date", "2026-02-29"),
    ("start_date", "2026-3-01"),
    ("start_date", "2026-03-01T00:00:00Z"),
    ("start_date", 20260301),
    ("end_date", False),
    ("end_date", []),
    ("end_date", "2026-04-31"),
    ("unexpected", "SECRET_CLINICAL"),
]


@pytest.mark.parametrize("field,value", INVALID_FIELDS)
def test_ac1_closed_admission_rejects_coercions_and_unknowns(
    command_setup, field, value
):
    h, rx = command_setup()
    intent = correction(h, rx, **{field: value})
    reads.assert_error(
        h.client.post(
            COMMANDS, {"kind": "date_correction", "intent": intent}, format="json"
        ),
        400,
        "invalid_request",
    )
    assert command_model().objects.count() == 0


@pytest.mark.parametrize(
    "field",
    [
        "mutation_epoch",
        "patient_id",
        "prescription_id",
        "expected_revision",
        "start_date",
        "end_date",
    ],
)
def test_ac1_every_correction_key_is_required(command_setup, field):
    h, rx = command_setup()
    intent = correction(h, rx)
    del intent[field]
    reads.assert_error(
        h.client.post(
            COMMANDS, {"kind": "date_correction", "intent": intent}, format="json"
        ),
        400,
        "invalid_request",
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"kind":"date_correction","intent":{"patient_id":NaN}}',
        '{"kind":"date_correction","intent":{"patient_id":Infinity}}',
        "[]",
        "null",
        "{",
    ],
)
def test_ac1_json_duplicates_nonfinite_and_nonobjects_rejected(command_setup, raw):
    h, _ = command_setup()
    reads.assert_error(
        h.client.post(COMMANDS, raw, content_type="application/json"),
        400,
        "invalid_request",
    )
    assert command_model().objects.count() == 0


@pytest.mark.parametrize("duplicate", ["kind", "start_date"])
def test_ac1_duplicate_keys_rejected_in_otherwise_valid_json(command_setup, duplicate):
    h, rx = command_setup()
    intent = correction(h, rx)
    body = json.dumps({"kind": "date_correction", "intent": intent})
    key_value = {
        "kind": '"kind": "date_correction"',
        "start_date": '"start_date": "2026-03-01"',
    }[duplicate]
    raw = body.replace(key_value, key_value + ", " + key_value)
    reads.assert_error(
        h.client.post(COMMANDS, raw, content_type="application/json"),
        400,
        "invalid_request",
    )
    assert command_model().objects.count() == 0
    if duplicate == "start_date":
        pending = admit(h, intent)
        raw = json.dumps(intent).replace(key_value, key_value + ", " + key_value)
        reads.assert_error(
            h.client.put(
                put_url(pending["command_id"]), raw, content_type="application/json"
            ),
            400,
            "invalid_request",
        )
        assert_command(
            h.client.get(f"{COMMANDS}{pending['command_id']}/"), 200, "pending"
        )


def test_ac1_put_canonicalizes_key_order_but_never_changes_intent(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    changed = {**intent, "end_date": "2026-04-02"}
    reads.assert_error(execute(h, pending, changed), 409, "command_conflict")
    reordered = dict(reversed(list(intent.items())))
    assert_command(execute(h, pending, reordered), 200, "succeeded")
    reads.assert_error(execute(h, pending, changed), 409, "command_conflict")


def test_rm3_pending_reserves_object_globally_without_exposing_other_idel(
    command_setup,
):
    h, rx = command_setup()
    other = reads.ExpiryAccount()
    other.nurse.patients.add(h.patient)
    intent = correction(h, rx)
    pending = admit(h, intent)
    other_intent = {
        **intent,
        "mutation_epoch": other.page()["context"]["mutation_epoch"],
    }
    reads.assert_error(
        other.client.post(
            COMMANDS, {"kind": "date_correction", "intent": other_intent}, format="json"
        ),
        409,
        "reconciliation_required",
    )
    assert other.client.get(COMMANDS).json()["results"] == []
    reads.assert_error(
        other.client.get(f"{COMMANDS}{pending['command_id']}/"), 404, "not_found"
    )
    assert command_model().objects.count() == 1


def test_rm2_current_rights_precede_command_and_version_diagnostics(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    other = reads.ExpiryAccount()
    other.user.is_staff = other.user.is_superuser = True
    other.user.save(update_fields=["is_staff", "is_superuser"])
    wrong = {**intent, "expected_revision": "wrong", "end_date": "2026-04-02"}
    absent = str(uuid.uuid4())
    for identifier in (pending["command_id"], absent):
        response = other.client.put(put_url(identifier), wrong, format="json")
        reads.assert_error(response, 404, "not_found")
    h.nurse.patients.remove(h.patient)
    reads.assert_error(execute(h, pending, wrong), 404, "not_found")
    reads.assert_error(
        h.client.get(f"{COMMANDS}{pending['command_id']}/"), 404, "not_found"
    )
    assert h.client.get(COMMANDS).json()["results"] == []


def test_rm2_pending_origin_session_cannot_transfer_but_terminal_is_interdevice(
    command_setup,
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    second, _ = new_client(h)
    assert (
        assert_command(
            second.get(f"{COMMANDS}{pending['command_id']}/"), 200, "pending"
        )
        == pending
    )
    reads.assert_error(
        execute(h, pending, intent, second), 409, "reconciliation_required"
    )
    result = assert_command(execute(h, pending, intent), 200, "succeeded")
    assert (
        assert_command(
            second.get(f"{COMMANDS}{pending['command_id']}/"), 200, "succeeded"
        )
        == result
    )
    assert (
        assert_command(execute(h, pending, intent, second), 200, "succeeded") == result
    )


def test_rm2_revoked_origin_session_does_not_hide_terminal_from_same_principal(
    command_setup,
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    result = assert_command(execute(h, pending, intent), 200, "succeeded")
    second, _ = new_client(h)
    AuthSession.objects.filter(pk=h.pair["session_id"]).update(
        revoked_at=timezone.now()
    )
    assert (
        assert_command(
            second.get(f"{COMMANDS}{pending['command_id']}/"), 200, "succeeded"
        )
        == result
    )
    assert (
        assert_command(execute(h, pending, intent, second), 200, "succeeded") == result
    )


def test_k20_recreated_principal_with_same_pk_and_email_cannot_recover_old_command(
    command_setup,
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    assert_command(execute(h, pending, intent), 200, "succeeded")
    old_user_id, username = h.user.pk, h.user.username
    h.user.delete()
    h.user = User.objects.create_user(
        pk=old_user_id, username=username, password=reads.PASSWORD
    )
    h.nurse = Nurse.objects.create(user=h.user)
    h.nurse.patients.add(h.patient)
    h.client, h.pair = new_client(h)
    assert h.page()["context"]["mutation_epoch"] != intent["mutation_epoch"]
    assert h.client.get(COMMANDS).json()["results"] == []
    reads.assert_error(
        h.client.get(f"{COMMANDS}{pending['command_id']}/"), 404, "not_found"
    )
    reads.assert_error(execute(h, pending, intent), 404, "not_found")


@pytest.mark.parametrize(
    "failure",
    ["session", "token", "expired", "fingerprint", "password", "inactive", "profile"],
)
def test_rm2_origin_authority_is_rechecked_before_execution(command_setup, failure):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    now = timezone.now()
    session = AuthSession.objects.filter(pk=h.pair["session_id"])
    token = AuthToken.objects.filter(pk=h.pair["access_token"].split(".")[0])
    changes = {
        "session": lambda: session.update(revoked_at=now),
        "token": lambda: token.update(revoked_at=now),
        "expired": lambda: token.update(
            issued_at=now - timedelta(hours=2), expires_at=now - timedelta(hours=1)
        ),
        "fingerprint": lambda: session.update(password_fingerprint="x" * 64),
        "password": lambda: User.objects.filter(pk=h.user.pk).update(
            password="changed"
        ),
        "inactive": lambda: User.objects.filter(pk=h.user.pk).update(is_active=False),
        "profile": lambda: Nurse.objects.filter(pk=h.nurse.pk).delete(),
    }
    before = (rx.start_date, rx.end_date)
    changes[failure]()
    response = execute(h, pending, intent)
    reads.assert_error(
        response,
        404 if failure == "profile" else 401,
        "not_found" if failure == "profile" else "authentication_required",
    )
    assert_unchanged(rx, before)


def test_rm2_admission_requires_bearer_not_cookie_or_legacy_token(command_setup):
    h, rx = command_setup()
    payload = {"kind": "date_correction", "intent": correction(h, rx)}
    client = APIClient()
    client.force_login(h.user)
    reads.assert_error(
        client.post(COMMANDS, payload, format="json"), 401, "authentication_required"
    )
    legacy = Token.objects.create(user=h.user)
    client.credentials(HTTP_AUTHORIZATION=f"Token {legacy.key}")
    reads.assert_error(
        client.post(COMMANDS, payload, format="json"), 401, "authentication_required"
    )


def test_ac3_journal_is_bounded_ordered_and_cursor_invalidates(command_setup):
    h, rx = command_setup()
    commands = []
    # Tied instants exercise command_id; separate instants exercise newest first.
    for index in range(22):
        with freeze_time(timezone.now() + timedelta(seconds=index // 3)):
            intent = correction(h, rx)
            commands.append(
                assert_command(execute(h, admit(h, intent), intent), 200, "succeeded")
            )
    response = h.client.get(COMMANDS)
    reads.assert_private(response)
    page = response.json()
    assert set(page) == JOURNAL_KEYS
    assert page["contract_revision"] == "0.2-decision"
    assert len(page["results"]) == 20
    cursor = page["next_cursor"]
    assert isinstance(cursor, str) and 0 < len(cursor) <= 2048
    second = h.client.get(COMMANDS, {"cursor": cursor}).json()
    assert len(second["results"]) == 2 and second["next_cursor"] is None
    rows = page["results"] + second["results"]
    assert {row["command_id"] for row in rows} == {
        row["command_id"] for row in commands
    }
    assert rows == sorted(
        sorted(rows, key=lambda row: row["command_id"]),
        key=lambda row: row["recorded_at"],
        reverse=True,
    )
    outsider = reads.ExpiryAccount()
    reads.assert_error(
        outsider.client.get(COMMANDS, {"cursor": cursor}), 400, "invalid_cursor"
    )
    fresh = correction(h, rx)
    admit(h, fresh)
    reads.assert_error(
        h.client.get(COMMANDS, {"cursor": cursor}), 409, "pagination_invalidated"
    )
    cursor = h.client.get(COMMANDS).json()["next_cursor"]
    h.nurse.patients.remove(h.patient)
    reads.assert_error(
        h.client.get(COMMANDS, {"cursor": cursor}), 409, "pagination_invalidated"
    )
    assert h.client.get(COMMANDS).json()["results"] == []
    reads.assert_error(
        h.client.get(f"{COMMANDS}{commands[0]['command_id']}/"), 404, "not_found"
    )


@pytest.mark.parametrize(
    "query",
    [
        "page_size=1",
        "patient_id=1",
        "account=1",
        "cursor=x&cursor=x",
        "ordering=recorded_at",
    ],
)
def test_ac3_journal_query_is_closed(command_setup, query):
    h, _ = command_setup()
    reads.assert_error(h.client.get(COMMANDS + "?" + query), 400, "invalid_request")


def test_ac3_get_pending_and_journal_never_resume_or_write(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    before = (rx.start_date, rx.end_date)
    with CaptureQueriesContext(connection) as queries:
        assert_command(
            h.client.get(f"{COMMANDS}{pending['command_id']}/"), 200, "pending"
        )
        assert h.client.get(COMMANDS).json()["results"] == [pending]
    assert not any(
        q["sql"].lstrip().split()[0].upper()
        in {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "CREATE", "ALTER"}
        for q in queries
    )
    assert_unchanged(rx, before)


def test_rm6_fence_closes_pending_and_old_forms_without_clinical_effect(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    before = (rx.start_date, rx.end_date)
    response = h.client.post(FENCE, {}, format="json")
    assert response.status_code == 200
    reads.assert_private(response)
    fence = response.json()
    assert set(fence) == {"contract_revision", "mutation_epoch", "journal_revision"}
    assert fence["contract_revision"] == "0.2-decision"
    assert fence["mutation_epoch"] != intent["mutation_epoch"]
    abandoned = assert_command(
        h.client.get(f"{COMMANDS}{pending['command_id']}/"), 200, "rejected"
    )
    assert abandoned["error"]["code"] == "command_abandoned"
    assert_unchanged(rx, before)
    reads.assert_error(
        h.client.post(
            COMMANDS, {"kind": "date_correction", "intent": intent}, format="json"
        ),
        409,
        "command_epoch_closed",
    )
    assert_command(execute(h, pending, intent), 200, "rejected")
    repeated = h.client.post(FENCE, {}, format="json").json()
    assert repeated["mutation_epoch"] != fence["mutation_epoch"]
    current = correction(h, rx)
    assert_command(execute(h, admit(h, current), current), 200, "succeeded")


@pytest.mark.parametrize("payload", [{"mutation_epoch": "old"}, [], None])
def test_ac3_fence_body_is_exact_empty_object(command_setup, payload):
    h, _ = command_setup()
    reads.assert_error(
        h.client.post(FENCE, json.dumps(payload), content_type="application/json"),
        400,
        "invalid_request",
    )


def test_rm2_regrant_never_resurrects_old_epoch_or_pending(command_setup):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    h.nurse.patients.remove(h.patient)
    h.nurse.patients.add(h.patient)
    assert h.detail(rx).json()["context"]["mutation_epoch"] != intent["mutation_epoch"]
    response = execute(h, pending, intent)
    assert response.status_code in (200, 409)
    if response.status_code == 200:
        assert (
            assert_command(response, 200, "rejected")["error"]["code"]
            == "command_abandoned"
        )
    else:
        reads.assert_error(response, 409, "command_epoch_closed")


def test_rm8_success_invalidates_all_associated_accounts_and_durable_futures(
    command_setup,
):
    h, rx = command_setup()
    other = reads.ExpiryAccount()
    other.nurse.patients.add(h.patient)
    old_a, old_b = h.page()["context"], other.page()["context"]
    intent = correction(h, rx)
    pending = admit(h, intent)
    assert_command(execute(h, pending, intent), 200, "succeeded")
    assert h.page()["context"]["collection_revision"] != old_a["collection_revision"]
    assert (
        other.page()["context"]["collection_revision"] != old_b["collection_revision"]
    )
    future = command_model("ExpiryFutureAuthority").objects.get(prescription_id=rx.pk)
    current = h.detail(rx).json()["item"]["revision"]
    assert (
        str(future.revision) == current
        and str(future.revision) != intent["expected_revision"]
    )
    assert future.generation > 0
    audit = command_model("ExpiryCorrectionAudit").objects.get(
        command_id=pending["command_id"]
    )
    assert audit.user_id == h.user.pk and audit.prescription_id == rx.pk
    assert audit.patient_id == h.patient.pk and audit.recorded_at is not None
    assert str(audit.before_revision) == intent["expected_revision"]
    assert str(audit.after_revision) == current


def test_journal_cursor_invalidates_when_legacy_writer_moves_prescription(
    command_setup,
):
    h, rx = command_setup()
    for _ in range(22):
        intent = correction(h, rx)
        assert_command(execute(h, admit(h, intent), intent), 200, "succeeded")
    cursor = h.client.get(COMMANDS).json()["next_cursor"]
    assert cursor
    other = reads.ExpiryAccount()
    # The actor's M2M/scope is unchanged; clinical movement changes visibility.
    Prescription.objects.filter(pk=rx.pk).update(patient_id=other.patient.pk)
    reads.assert_error(
        h.client.get(COMMANDS, {"cursor": cursor}), 409, "pagination_invalidated"
    )
    assert h.client.get(COMMANDS).json()["results"] == []


@pytest.mark.parametrize("damage", ["ciphertext", "metadata", "historical_key"])
def test_corrupt_pending_intent_or_missing_key_has_no_effect(
    command_setup, settings, damage
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    before = (rx.start_date, rx.end_date)
    if damage == "ciphertext":
        command_model().objects.filter(pk=pending["command_id"]).update(
            payload_ciphertext=b"invalid-ciphertext"
        )
    elif damage == "metadata":
        another = h.prescription()
        command_model().objects.filter(pk=pending["command_id"]).update(
            prescription_id=another.pk
        )
    else:
        settings.EXPIRY_COMMAND_ACTIVE_KEY = "v2"
        settings.EXPIRY_COMMAND_KEYS = {
            "v2": {
                "encryption": base64.b64encode(b"e" * 32).decode(),
                "hmac": base64.b64encode(b"h" * 32).decode(),
            }
        }
    reads.assert_error(execute(h, pending, intent), 503, "service_unavailable")
    assert_unchanged(rx, before)
    row = command_model().objects.get(pk=pending["command_id"])
    assert row.state == "pending" and row.terminal_at is None
    assert not command_model("ExpiryCorrectionAudit").objects.exists()


@pytest.mark.parametrize("point", ["clinical", "terminal"])
def test_expiration_during_clinical_transaction_rolls_back_every_effect(
    command_setup, point
):
    h, rx = command_setup()
    intent = correction(h, rx)
    pending = admit(h, intent)
    before = (rx.start_date, rx.end_date)
    context_before = h.page()["context"]
    futures_before = list(command_model("ExpiryFutureAuthority").objects.values())
    token = AuthToken.objects.get(pk=h.pair["access_token"].split(".")[0])
    hit = []
    with freeze_time(timezone.now()) as clock:

        def expire_before_write(execute_sql, sql, params, many, context):
            statement = sql.lstrip().upper()
            clinical = statement.startswith('UPDATE "NURSE_PRESCRIPTION" ')
            terminal = statement.startswith(
                'UPDATE "NURSE_EXPIRYCOMMAND" '
            ) and "succeeded" in (params or ())
            if {"clinical": clinical, "terminal": terminal}[point]:
                hit.append(True)
                clock.move_to(token.expires_at + timedelta(seconds=1))
            return execute_sql(sql, params, many, context)

        with connection.execute_wrapper(expire_before_write):
            response = execute(h, pending, intent)
        assert hit == [True]
        reads.assert_error(response, 401, "authentication_required")
    assert_unchanged(rx, before)
    assert h.page()["context"] == context_before
    assert (
        list(command_model("ExpiryFutureAuthority").objects.values()) == futures_before
    )
    row = command_model().objects.get(pk=pending["command_id"])
    assert row.state == "pending" and row.result is None and row.terminal_at is None
    assert not command_model("ExpiryCorrectionAudit").objects.exists()


@pytest.mark.parametrize("writer", ["sql", "bulk", "document", "delete"])
def test_legacy_writers_invalidate_durable_future_authority_when_closed(
    command_setup, writer
):
    h, rx = command_setup()
    command_model("ExpiryCommandAuthority").objects.update(accepting=False)
    future_model = command_model("ExpiryFutureAuthority")
    before = future_model.objects.get(prescription_id=rx.pk)
    if writer == "sql":
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE nurse_prescription SET end_date=%s WHERE id=%s",
                ["2026-05-01", rx.pk],
            )
    elif writer == "bulk":
        rx.end_date = date(2026, 5, 1)
        Prescription.objects.bulk_update([rx], ["end_date"])
    elif writer == "document":
        reads.make_document_version(h, rx)
    else:
        Prescription.objects.filter(pk=rx.pk).delete()
    after = future_model.objects.get(prescription_id=rx.pk)
    assert after.revision != before.revision and after.generation > before.generation
    if writer != "delete":
        assert str(after.revision) == h.detail(rx).json()["item"]["revision"]
    assert not command_model("ExpiryCorrectionAudit").objects.exists()
