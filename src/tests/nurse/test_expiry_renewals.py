"""#282 AC1–7 / RM1–8: explicit human renewal, wire 0.2-decision.

HTTP assertions are contractual. ExpiryRenewal is the combined durable relation
and clinical audit seam; its field names are recorded in the test handoff.
"""

import json
import uuid
from datetime import timedelta

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from freezegun import freeze_time
from rest_framework.test import APIClient

from main.models import AuthSession, AuthToken
from nurse.models import Prescription
from tests.nurse import test_expiry_commands as commands
from tests.nurse import test_expiry_reads_v4 as reads
from tests.nurse.test_document_reads_v3 import PNG
from tests.nurse.test_expiry_command_retention import maintenance

pytestmark = pytest.mark.django_db
expiry_settings = commands.expiry_settings
command_settings = commands.command_settings
command_setup = commands.command_setup
DECLARATIONS = (
    "patient_reviewed",
    "pages_reviewed",
    "dates_reviewed",
    "replacement_confirmed",
)
RENEWAL_KEYS = {
    "mutation_epoch",
    "patient_id",
    "old_prescription_id",
    "new_prescription_id",
    "expected_old_revision",
    "expected_new_revision",
    "reviewed_document_version",
    *DECLARATIONS,
}


@pytest.fixture
def renewal_settings(command_settings, settings):
    settings.EXPIRY_RENEWALS_ENABLED = True
    return settings


@pytest.fixture
def renewal_setup(renewal_settings, command_setup):
    def create(**new_dates):
        account, old = command_setup()
        new = account.prescription(**new_dates)
        make_source(account, new)
        return account, old, new

    return create


def make_source(account, prescription, number=1):
    imported, version, asset = reads.make_document_version(
        account, prescription, version_number=number
    )
    saved = default_storage.save(asset.storage_key, ContentFile(PNG))
    assert saved == asset.storage_key
    return imported, version, asset


def relation_model():
    return commands.command_model("ExpiryRenewal")


def renewal(account, old, new, **changes):
    old_detail, new_detail = account.detail(old), account.detail(new)
    assert old_detail.status_code == new_detail.status_code == 200
    old_body, new_body = old_detail.json(), new_detail.json()
    return {
        "mutation_epoch": old_body["context"]["mutation_epoch"],
        "patient_id": old.patient_id,
        "old_prescription_id": old.pk,
        "new_prescription_id": new.pk,
        "expected_old_revision": old_body["item"]["revision"],
        "expected_new_revision": new_body["item"]["revision"],
        "reviewed_document_version": new_body["item"]["document_version"],
        **dict.fromkeys(DECLARATIONS, True),
        **changes,
    }


def put_url(identifier):
    return f"{commands.COMMANDS}{identifier}/renewal-confirmation/"


def post(account, intent, client=None):
    return (client or account.client).post(
        commands.COMMANDS,
        {"kind": "renewal_confirmation", "intent": intent},
        format="json",
    )


def assert_command(response, status, state):
    assert response.status_code == status, response.content
    reads.assert_private(response)
    body = response.json()
    assert set(body) == commands.COMMAND_KEYS
    identifier = uuid.UUID(body["command_id"])
    assert identifier.version == 4 and str(identifier) == body["command_id"]
    assert body["kind"] == "renewal_confirmation" and body["state"] == state
    assert body["recorded_at"].endswith("Z")
    assert isinstance(body["mutation_epoch"], str) and body["mutation_epoch"]
    if state == "succeeded":
        assert set(body["result"]) == commands.RESULT_KEYS
        assert type(body["result"]["replacement_prescription_id"]) is int
        assert body["error"] is None
    elif state == "rejected":
        assert body["result"] is None
        assert set(body["error"]) == {"code", "message", "fields"}
    else:
        assert body["result"] is body["error"] is None
    return body


def admit(account, intent, client=None):
    return assert_command(post(account, intent, client), 201, "pending")


def execute(account, pending, intent, client=None):
    return (client or account.client).put(
        put_url(pending["command_id"]), intent, format="json"
    )


def confirm(account, old, new):
    intent = renewal(account, old, new)
    pending = admit(account, intent)
    return assert_command(execute(account, pending, intent), 200, "succeeded")


def snapshot(account, *prescriptions):
    """Observable clinical state, revision and future authorities, no command TTL."""
    ids = [row.pk for row in prescriptions]
    return {
        "clinical": list(
            Prescription.objects.filter(pk__in=ids).order_by("pk").values()
        ),
        "items": [account.detail(row).json()["item"] for row in prescriptions],
        "context": account.page()["context"],
        "futures": list(
            commands.command_model("ExpiryFutureAuthority")
            .objects.filter(prescription_id__in=ids)
            .order_by("pk")
            .values()
        ),
    }


def test_ac1_admission_and_get_reserve_without_clinical_effect(renewal_setup):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    before = snapshot(account, old, new)
    pending = admit(account, intent)
    assert pending["mutation_epoch"] == intent["mutation_epoch"]
    row = commands.command_model().objects.get(pk=pending["command_id"])
    assert row.prescription_id == old.pk and row.new_prescription_id == new.pk
    assert row.user_id == account.user.pk
    assert str(row.origin_session_id) == account.pair["session_id"]
    assert row.payload_ciphertext and row.payload_hmac and row.terminal_at is None
    with CaptureQueriesContext(connection) as queries:
        response = account.client.get(f"{commands.COMMANDS}{pending['command_id']}/")
        assert assert_command(response, 200, "pending") == pending
        assert account.client.get(commands.COMMANDS).json()["results"] == [pending]
    assert not any(
        query["sql"].lstrip().split()[0].upper()
        in {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "CREATE", "ALTER"}
        for query in queries
    )
    assert snapshot(account, old, new) == before
    assert not relation_model().objects.exists()


def test_rm5_success_is_atomic_minimal_audited_and_replay_has_one_effect(renewal_setup):
    account, old, new = renewal_setup(start_date=None, end_date=None)
    other = reads.ExpiryAccount()
    other.nurse.patients.add(account.patient)
    intent = renewal(account, old, new)
    before = snapshot(account, old, new)
    other_context = other.page()["context"]
    document_before = list(new.document_versions.values())
    pending = admit(account, intent)
    terminal = assert_command(execute(account, pending, intent), 200, "succeeded")
    old_item, new_item = (account.detail(row).json()["item"] for row in (old, new))
    assert old_item["followup_state"] == "renewed" and not old_item["to_renew"]
    assert set(old_item["replacement"]) == {"prescription_id", "confirmed_at"}
    assert old_item["replacement"]["prescription_id"] == new.pk
    assert old_item["replacement"]["confirmed_at"].endswith("Z")
    assert new_item["followup_state"] == "to_process"
    assert new_item["replacement"] is None and new_item["date_state"] == "unknown"
    assert new_item["start_date"] is new_item["end_date"] is None
    for item, key in ((old_item, "old"), (new_item, "new")):
        assert item["revision"] != intent[f"expected_{key}_revision"]
    assert (
        list(
            Prescription.objects.filter(pk__in=[old.pk, new.pk]).order_by("pk").values()
        )
        == before["clinical"]
    )
    assert list(new.document_versions.values()) == document_before
    assert terminal["result"] == {
        "patient_id": account.patient.pk,
        "prescription_id": old.pk,
        "revision": old_item["revision"],
        "replacement_prescription_id": new.pk,
        "collection_revision": account.page()["context"]["collection_revision"],
    }
    assert (
        terminal["result"]["collection_revision"]
        != before["context"]["collection_revision"]
    )
    assert (
        other.page()["context"]["collection_revision"]
        != other_context["collection_revision"]
    )
    for item in (old_item, new_item):
        previous = next(
            row for row in before["futures"] if row["prescription_id"] == item["id"]
        )
        future = commands.command_model("ExpiryFutureAuthority").objects.get(
            prescription_id=item["id"]
        )
        assert str(future.revision) == item["revision"] != str(previous["revision"])
        assert future.generation > previous["generation"]
    audit = relation_model().objects.get(command_id=pending["command_id"])
    assert audit.old_prescription_id == old.pk and audit.new_prescription_id == new.pk
    assert audit.patient_id == account.patient.pk and audit.user_id == account.user.pk
    assert str(audit.origin_session_id) == account.pair["session_id"]
    assert audit.recorded_at == timezone.now()
    assert audit.reviewed_document_version == intent["reviewed_document_version"]
    assert all(getattr(audit, field) is True for field in DECLARATIONS)
    for item, key in ((old_item, "old"), (new_item, "new")):
        assert (
            str(getattr(audit, f"before_{key}_revision"))
            == intent[f"expected_{key}_revision"]
        )
        assert str(getattr(audit, f"after_{key}_revision")) == item["revision"]
    row = commands.command_model().objects.get(pk=pending["command_id"])
    assert row.payload_ciphertext is None and row.terminal_at == timezone.now()
    assert row.payload_hmac and row.result == terminal["result"]
    after = snapshot(account, old, new)
    assert (
        assert_command(execute(account, pending, intent), 200, "succeeded") == terminal
    )
    assert snapshot(account, old, new) == after
    assert relation_model().objects.count() == 1


INVALID_FIELDS = [
    ("patient_id", "1"),
    ("old_prescription_id", True),
    ("new_prescription_id", 1.0),
    ("old_prescription_id", 0),
    ("new_prescription_id", 9007199254740992),
    ("patient_id", -1),
    ("expected_old_revision", ""),
    ("expected_new_revision", "r" * 129),
    ("expected_new_revision", 1),
    ("mutation_epoch", None),
    ("reviewed_document_version", True),
    ("reviewed_document_version", "1"),
    ("reviewed_document_version", 0),
    ("reviewed_document_version", 1.0),
    ("unexpected", "SECRET_CLINICAL_282"),
]


@pytest.mark.parametrize("field,value", INVALID_FIELDS)
def test_rm2_closed_intent_rejects_coercion_and_unknown_fields(
    renewal_setup, field, value
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new, **{field: value})
    reads.assert_error(post(account, intent), 400, "invalid_request")
    assert not commands.command_model().objects.exists()


@pytest.mark.parametrize("field", sorted(RENEWAL_KEYS))
def test_rm2_every_renewal_key_is_required(renewal_setup, field):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    del intent[field]
    reads.assert_error(post(account, intent), 400, "invalid_request")
    assert not commands.command_model().objects.exists()


@pytest.mark.parametrize("field", DECLARATIONS)
@pytest.mark.parametrize("value", [False, 0, 1, "true", "false", None, [], {}])
def test_rm2_each_human_declaration_requires_literal_true(renewal_setup, field, value):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new, **{field: value})
    reads.assert_error(post(account, intent), 400, "invalid_request")
    assert not commands.command_model().objects.exists()


def test_rm2_no_get_or_import_can_supply_missing_human_confirmation(renewal_setup):
    account, old, new = renewal_setup()
    assert account.detail(new).json()["item"]["document_version"] == 1
    assert account.detail(old).json()["item"]["followup_state"] == "to_process"
    intent = renewal(account, old, new)
    for field in DECLARATIONS:
        del intent[field]
    reads.assert_error(post(account, intent), 400, "invalid_request")
    assert account.detail(old).json()["item"]["followup_state"] == "to_process"


@pytest.mark.parametrize("duplicate", ["kind", "new_prescription_id", "pages_reviewed"])
def test_rm2_duplicate_json_keys_are_rejected_on_admission_and_execute(
    renewal_setup, duplicate
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    body = {"kind": "renewal_confirmation", "intent": intent}
    fragment = (
        json.dumps(duplicate)
        + ": "
        + json.dumps(body.get(duplicate, intent.get(duplicate)))
    )
    raw = json.dumps(body).replace(fragment, fragment + ", " + fragment)
    reads.assert_error(
        account.client.post(commands.COMMANDS, raw, content_type="application/json"),
        400,
        "invalid_request",
    )
    assert not commands.command_model().objects.exists()
    if duplicate != "kind":
        pending = admit(account, intent)
        raw = json.dumps(intent).replace(fragment, fragment + ", " + fragment)
        reads.assert_error(
            account.client.put(
                put_url(pending["command_id"]), raw, content_type="application/json"
            ),
            400,
            "invalid_request",
        )
        assert_command(
            account.client.get(f"{commands.COMMANDS}{pending['command_id']}/"),
            200,
            "pending",
        )


def test_rm4_put_is_lookup_only_kind_bound_and_canonical_immutable(renewal_setup):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    reads.assert_error(
        account.client.put(put_url(uuid.uuid4()), intent, format="json"),
        404,
        "not_found",
    )
    assert not commands.command_model().objects.exists()
    pending = admit(account, intent)
    changed = {**intent, "reviewed_document_version": 2}
    reads.assert_error(execute(account, pending, changed), 409, "command_conflict")
    correction = commands.correction(account, old)
    reads.assert_error(
        commands.execute(account, pending, correction), 409, "command_conflict"
    )
    terminal = assert_command(
        execute(account, pending, dict(reversed(list(intent.items())))),
        200,
        "succeeded",
    )
    reads.assert_error(execute(account, pending, changed), 409, "command_conflict")
    assert (
        assert_command(execute(account, pending, intent), 200, "succeeded") == terminal
    )


@pytest.mark.parametrize("target", ["same", "foreign", "absent"])
def test_rm1_objects_must_be_distinct_registered_same_authorized_patient(
    renewal_setup, target
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    outsider = reads.ExpiryAccount()
    foreign = outsider.prescription()
    targets = {"same": old.pk, "foreign": foreign.pk, "absent": 9007199254740991}
    intent["new_prescription_id"] = targets[target]
    # Even association to both patients does not permit cross-patient renewal.
    account.nurse.patients.add(outsider.patient)
    intent["mutation_epoch"] = account.page()["context"]["mutation_epoch"]
    reads.assert_error(
        post(account, intent),
        400 if target == "same" else 404,
        "invalid_request" if target == "same" else "not_found",
    )
    assert not commands.command_model().objects.exists()


@pytest.mark.parametrize("revision", ["old", "new"])
def test_rm2_both_exact_revisions_are_required_and_rejection_replays(
    renewal_setup, revision
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new, **{f"expected_{revision}_revision": "obsolete"})
    before = snapshot(account, old, new)
    pending = admit(account, intent)
    terminal = assert_command(execute(account, pending, intent), 200, "rejected")
    assert terminal["error"]["code"] == "revision_conflict"
    assert snapshot(account, old, new) == before
    assert (
        assert_command(execute(account, pending, intent), 200, "rejected") == terminal
    )
    row = commands.command_model().objects.get(pk=pending["command_id"])
    assert row.payload_ciphertext is None and row.terminal_at is not None
    assert not relation_model().objects.exists()


@pytest.mark.parametrize(
    "damage", ["file", "metadata", "version", "dates", "pages", "association"]
)
def test_rm2_source_must_still_be_current_available_and_unchanged(
    renewal_setup, damage
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    pending = admit(account, intent)
    imported = new.document_versions.get().document_import
    asset = new.document_versions.get().assets.get()
    changes = {
        "file": lambda: default_storage.delete(asset.storage_key),
        "metadata": lambda: type(imported)
        .objects.filter(pk=imported.pk)
        .update(metadata={"pages": []}),
        "version": lambda: make_source(account, new, number=2),
        "dates": lambda: Prescription.objects.filter(pk=new.pk).update(
            end_date="2026-05-01"
        ),
        "pages": lambda: type(asset).objects.filter(pk=asset.pk).update(page_count=2),
        "association": lambda: type(imported)
        .objects.filter(pk=imported.pk)
        .update(prescription_id=old.pk),
    }
    changes[damage]()
    before = snapshot(account, old, new)
    terminal = assert_command(execute(account, pending, intent), 200, "rejected")
    assert terminal["error"]["code"] == (
        "source_unavailable" if damage == "file" else "revision_conflict"
    )
    assert snapshot(account, old, new) == before
    assert not relation_model().objects.exists()


def test_rm2_reviewed_version_mismatch_and_missing_source_are_terminal(renewal_setup):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new, reviewed_document_version=2)
    pending = admit(account, intent)
    terminal = assert_command(execute(account, pending, intent), 200, "rejected")
    assert terminal["error"]["code"] == "source_unavailable"
    assert not relation_model().objects.exists()


@pytest.mark.parametrize(
    "first_pair,second_pair,allowed",
    [
        ((0, 1), (1, 2), True),
        ((1, 2), (0, 1), False),
        ((0, 1), (0, 2), False),
        ((0, 1), (1, 0), False),
        ((0, 1), (2, 1), False),
        ((0, 1), (0, 1), False),
    ],
)
def test_rm3_unique_relation_and_chronological_explicit_chains(
    renewal_setup, first_pair, second_pair, allowed
):
    account, old, new = renewal_setup()
    third = account.prescription()
    make_source(account, old)
    make_source(account, third)
    objects = (old, new, third)
    confirm(account, *(objects[index] for index in first_pair))
    source, target = (objects[index] for index in second_pair)
    intent = renewal(account, source, target)
    before = snapshot(account, *objects)
    pending = admit(account, intent)
    terminal = assert_command(
        execute(account, pending, intent), 200, "succeeded" if allowed else "rejected"
    )
    if allowed:
        assert relation_model().objects.count() == 2
        assert account.detail(new).json()["item"]["followup_state"] == "renewed"
        assert account.detail(third).json()["item"]["followup_state"] == "to_process"
    else:
        assert terminal["error"]["code"] == "state_conflict"
        assert snapshot(account, *objects) == before
        assert relation_model().objects.count() == 1


@pytest.mark.parametrize("held_kind", ["renewal", "correction"])
@pytest.mark.parametrize("shared", ["old", "new"])
@pytest.mark.parametrize("contender_kind", ["renewal", "correction"])
def test_rm4_pending_reserves_both_objects_across_kind_and_idel(
    renewal_setup, held_kind, shared, contender_kind
):
    account, old, new = renewal_setup()
    selected = {"old": old, "new": new}[shared]
    other = reads.ExpiryAccount()
    other.nurse.patients.add(account.patient)
    if held_kind == "renewal":
        admit(account, renewal(account, old, new))
    else:
        commands.admit(account, commands.correction(account, selected))
    other_intent = renewal(account, old, new)
    other_intent["mutation_epoch"] = other.page()["context"]["mutation_epoch"]
    if contender_kind == "renewal":
        response = post(other, other_intent)
    else:
        other_intent = commands.correction(
            account, selected, mutation_epoch=other_intent["mutation_epoch"]
        )
        response = other.client.post(
            commands.COMMANDS,
            {"kind": "date_correction", "intent": other_intent},
            format="json",
        )
    reads.assert_error(response, 409, "reconciliation_required")
    assert commands.command_model().objects.count() == 1
    assert other.client.get(commands.COMMANDS).json()["results"] == []


def test_rm1_other_associated_idel_can_confirm_without_creator_privilege(renewal_setup):
    creator, old, new = renewal_setup()
    actor = reads.ExpiryAccount()
    actor.nurse.patients.add(creator.patient)
    # Helpers use the original patient's path; identity is the second IDEL.
    creator.client, creator.user, creator.pair = actor.client, actor.user, actor.pair
    result = confirm(creator, old, new)
    audit = relation_model().objects.get(command_id=result["command_id"])
    assert audit.user_id == actor.user.pk


@pytest.mark.parametrize("state", ["pending", "succeeded"])
@pytest.mark.parametrize("removed", ["old", "new"])
def test_rm7_current_right_on_each_object_precedes_conflict_and_journal(
    renewal_setup, state, removed
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    pending = admit(account, intent)
    if state == "succeeded":
        assert_command(execute(account, pending, intent), 200, "succeeded")
    outsider = reads.ExpiryAccount()
    selected = {"old": old, "new": new}[removed]
    Prescription.objects.filter(pk=selected.pk).update(patient_id=outsider.patient.pk)
    wrong = {**intent, "expected_new_revision": "wrong"}
    for identifier in (pending["command_id"], str(uuid.uuid4())):
        reads.assert_error(
            account.client.put(put_url(identifier), wrong, format="json"),
            404,
            "not_found",
        )
        reads.assert_error(
            account.client.get(f"{commands.COMMANDS}{identifier}/"), 404, "not_found"
        )
    assert account.client.get(commands.COMMANDS).json()["results"] == []
    outsider.user.is_staff = outsider.user.is_superuser = True
    outsider.user.save(update_fields=["is_staff", "is_superuser"])
    reads.assert_error(execute(outsider, pending, intent), 404, "not_found")


def test_rm4_origin_session_does_not_transfer_and_terminal_replays_interdevice(
    renewal_setup,
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    pending = admit(account, intent)
    second, _ = commands.new_client(account)
    assert (
        assert_command(
            second.get(f"{commands.COMMANDS}{pending['command_id']}/"), 200, "pending"
        )
        == pending
    )
    reads.assert_error(
        execute(account, pending, intent, second), 409, "reconciliation_required"
    )
    terminal = assert_command(execute(account, pending, intent), 200, "succeeded")
    assert (
        assert_command(execute(account, pending, intent, second), 200, "succeeded")
        == terminal
    )


@pytest.mark.parametrize(
    "failure", ["session", "expired", "password", "inactive", "profile", "token"]
)
def test_rm1_origin_authority_is_rechecked_for_renewal(renewal_setup, failure):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    pending = admit(account, intent)
    before = list(Prescription.objects.order_by("pk").values())
    session = AuthSession.objects.filter(pk=account.pair["session_id"])
    changes = {
        "session": lambda: session.update(revoked_at=timezone.now()),
        "expired": lambda: session.update(
            created_at=timezone.now() - timedelta(seconds=2),
            expires_at=timezone.now() - timedelta(seconds=1),
        ),
        "password": lambda: type(account.user)
        .objects.filter(pk=account.user.pk)
        .update(password="synthetic-changed"),
        "inactive": lambda: type(account.user)
        .objects.filter(pk=account.user.pk)
        .update(is_active=False),
        "profile": lambda: account.nurse.delete(),
        "token": lambda: AuthToken.objects.filter(
            session_id=account.pair["session_id"], kind="access"
        ).update(revoked_at=timezone.now()),
    }
    changes[failure]()
    reads.assert_error(
        execute(account, pending, intent),
        404 if failure == "profile" else 401,
        "not_found" if failure == "profile" else "authentication_required",
    )
    assert list(Prescription.objects.order_by("pk").values()) == before
    assert not relation_model().objects.exists()


def test_rm7_fence_reaper_and_restore_release_both_without_effect(
    renewal_setup, settings
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    pending = admit(account, intent)
    before = snapshot(account, old, new)
    commands.command_model().objects.filter(pk=pending["command_id"]).update(
        recorded_at=timezone.now() - timedelta(days=40)
    )
    assert_command(
        account.client.get(f"{commands.COMMANDS}{pending['command_id']}/"),
        200,
        "pending",
    )
    assert account.client.post(commands.FENCE, {}, format="json").status_code == 200
    abandoned = assert_command(execute(account, pending, intent), 200, "rejected")
    assert abandoned["error"]["code"] == "command_abandoned"
    assert snapshot(account, old, new)["clinical"] == before["clinical"]
    for row in (old, new):
        correction = commands.correction(account, row)
        commands.assert_command(
            commands.execute(account, commands.admit(account, correction), correction),
            200,
            "succeeded",
        )
    fresh_intent = renewal(account, old, new)
    fresh = admit(account, fresh_intent)
    settings.EXPIRY_COMMAND_NAMESPACE = str(uuid.uuid4())
    maintenance("close_restored_expiry_commands")
    assert (
        not commands.command_model("ExpiryCommandAuthority").objects.get(pk=1).accepting
    )
    assert not relation_model().objects.exists()
    row = commands.command_model().objects.get(pk=fresh["command_id"])
    assert row.state == "rejected" and row.error["code"] == "command_abandoned"
    assert row.payload_ciphertext is None
    reads.assert_error(
        execute(account, fresh, fresh_intent), 503, "service_unavailable"
    )


def test_rm7_technical_purge_preserves_relation_clinical_audit_and_never_readmits(
    renewal_setup,
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    pending = admit(account, intent)
    assert_command(execute(account, pending, intent), 200, "succeeded")
    relation_before = list(relation_model().objects.values())
    before = snapshot(account, old, new)
    terminal_at = (
        commands.command_model().objects.get(pk=pending["command_id"]).terminal_at
    )
    with freeze_time(terminal_at + timedelta(days=30, seconds=1)):
        maintenance("purge_expiry_commands")
        maintenance("purge_expiry_commands")
        assert (
            not commands.command_model()
            .objects.filter(pk=pending["command_id"])
            .exists()
        )
        original_client = account.client
        try:
            account.client, _ = commands.new_client(account)
            reads.assert_error(execute(account, pending, intent), 404, "not_found")
            current = renewal(account, old, new)
            reads.assert_error(execute(account, pending, current), 404, "not_found")
        finally:
            account.client = original_client
    assert list(relation_model().objects.values()) == relation_before
    after = snapshot(account, old, new)
    assert after["clinical"] == before["clinical"] and after["items"] == before["items"]


@pytest.mark.parametrize(
    "dates,classification",
    [
        ((None, None), "unknown"),
        ((None, "2026-04-01"), "unknown"),
        (("2026-04-01", "2026-04-20"), "future"),
    ],
)
def test_rm5_new_unknown_or_future_dates_are_preserved(
    renewal_setup, dates, classification
):
    account, old, new = renewal_setup(start_date=dates[0], end_date=dates[1])
    confirm(account, old, new)
    item = account.detail(new).json()["item"]
    assert (item["start_date"], item["end_date"]) == dates
    assert (
        item["date_state"] == classification and item["followup_state"] == "to_process"
    )
    assert item["to_renew"] is False and item["proximity"] == "none"


def test_rm8_renewed_filter_precedes_count_pagination_and_old_correction_is_closed(
    renewal_setup,
):
    account, old, new = renewal_setup()
    for index in range(22):
        account.prescription(end_date=reads.DAY - timedelta(days=30 - index))
    first_page = account.page(page_size=1)
    cursor = first_page["next_cursor"]
    confirm(account, old, new)
    page = account.page(filter="renewed", page_size=1)
    assert page["count"] == 1 and page["next_cursor"] is None
    assert [item["id"] for item in page["results"]] == [old.pk]
    assert old.pk not in {
        item["id"] for item in account.page(filter="to_renew")["results"]
    }
    assert account.page(filter="all")["count"] == 24
    reads.assert_error(
        account.client.get(reads.LIST, {"page_size": 1, "cursor": cursor}),
        409,
        "pagination_invalidated",
    )
    detail = account.detail(old).json()
    assert detail["can_correct_dates"] is detail["can_confirm_renewal"] is False
    intent = commands.correction(account, old)
    pending = commands.admit(account, intent)
    rejected = commands.assert_command(
        commands.execute(account, pending, intent), 200, "rejected"
    )
    assert rejected["error"]["code"] == "state_conflict"


def test_ac7_renewal_flag_closes_mutation_but_keeps_corrections_open(
    renewal_setup, settings
):
    account, old, new = renewal_setup()
    assert account.detail(old).json()["can_confirm_renewal"] is True
    intent = renewal(account, old, new)
    pending = admit(account, intent)
    settings.EXPIRY_RENEWALS_ENABLED = False
    assert account.detail(old).json()["can_confirm_renewal"] is False
    assert account.detail(old).json()["can_correct_dates"] is True
    reads.assert_error(execute(account, pending, intent), 503, "service_unavailable")
    reads.assert_error(post(account, intent), 503, "service_unavailable")
    assert_command(
        account.client.get(f"{commands.COMMANDS}{pending['command_id']}/"),
        200,
        "pending",
    )
    assert not relation_model().objects.exists()


def test_ac7_renewal_route_is_bearer_only_private_closed_and_cors_qualified(
    renewal_setup, settings, caplog
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    anonymous = APIClient()
    reads.assert_error(
        anonymous.put(put_url(uuid.uuid4()), intent, format="json"),
        401,
        "authentication_required",
    )
    pending = admit(account, intent)
    reads.assert_error(
        account.client.put(
            put_url(pending["command_id"]) + "?patient_id=1", intent, format="json"
        ),
        400,
        "invalid_request",
    )
    reads.assert_error(
        account.client.post(put_url(pending["command_id"]), intent, format="json"),
        405,
        "method_not_allowed",
    )
    settings.CORS_ALLOWED_ORIGINS = ["https://synthetic.example.invalid"]
    response = account.client.options(
        put_url(pending["command_id"]),
        HTTP_ORIGIN="https://synthetic.example.invalid",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="PUT",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type",
    )
    assert response.status_code == 200
    reads.assert_private(response)
    assert (
        response["Access-Control-Allow-Origin"] == "https://synthetic.example.invalid"
    )
    assert "PUT" in response["Access-Control-Allow-Methods"]
    terminal = assert_command(execute(account, pending, intent), 200, "succeeded")
    for secret in (
        account.patient.firstname,
        account.user.username,
        account.pair["access_token"],
        pending["command_id"],
    ):
        assert secret not in caplog.text
    assert "patient_reviewed" not in json.dumps(terminal)


def test_ac7_openapi_exposes_exact_closed_renewal_and_kind_union(expiry_settings):
    schema = SchemaGenerator().get_schema(request=None, public=True)

    def resolve(value):
        while "$ref" in value:
            value = schema["components"]["schemas"][value["$ref"].split("/")[-1]]
        return value

    path = commands.COMMANDS + "{command_id}/renewal-confirmation/"
    assert path in schema["paths"], "#282 renewal PUT must be documented"
    assert set(schema["paths"][path]) == {"put"}
    operation = schema["paths"][path]["put"]
    body = resolve(operation["requestBody"]["content"]["application/json"]["schema"])
    assert set(body["properties"]) == set(body["required"]) == RENEWAL_KEYS
    assert body["additionalProperties"] is False
    for key in DECLARATIONS:
        field = resolve(body["properties"][key])
        assert field["type"] == "boolean" and field.get("enum") == [True]
    for key in (
        "patient_id",
        "old_prescription_id",
        "new_prescription_id",
        "reviewed_document_version",
    ):
        field = resolve(body["properties"][key])
        assert field["type"] == "integer" and field["minimum"] == 1
        if key != "reviewed_document_version":
            assert field["maximum"] == 9007199254740991
    for status in ("200", "202"):
        result = resolve(
            operation["responses"][status]["content"]["application/json"]["schema"]
        )
        assert (
            set(result["properties"])
            == set(result["required"])
            == commands.COMMAND_KEYS
        )
        assert result["additionalProperties"] is False
        kind = resolve(result["properties"]["kind"])
        assert set(kind["enum"]) == {"date_correction", "renewal_confirmation"}
    admission = resolve(
        schema["paths"][commands.COMMANDS]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
    )
    variants = admission.get("oneOf", [])
    assert len(variants) == 2, "Admission must bind each kind to its own intent schema"
    expected = {
        "date_correction": {
            "mutation_epoch",
            "patient_id",
            "prescription_id",
            "expected_revision",
            "start_date",
            "end_date",
        },
        "renewal_confirmation": RENEWAL_KEYS,
    }
    observed = set()
    for variant in variants:
        variant = resolve(variant)
        assert (
            set(variant["properties"]) == set(variant["required"]) == {"kind", "intent"}
        )
        assert variant["additionalProperties"] is False
        kind = resolve(variant["properties"]["kind"])["enum"]
        assert len(kind) == 1 and kind[0] in expected
        observed.add(kind[0])
        intent = resolve(variant["properties"]["intent"])
        assert set(intent["properties"]) == set(intent["required"]) == expected[kind[0]]
        assert intent["additionalProperties"] is False
    assert observed == set(expected)


@pytest.mark.parametrize(
    "shared_position", ["old_as_new", "new_as_old", "old_as_old", "new_as_new"]
)
def test_rm4_renewal_reservation_blocks_every_overlap_position(
    renewal_setup, shared_position
):
    account, old, new = renewal_setup()
    third = account.prescription()
    make_source(account, old)
    make_source(account, third)
    admit(account, renewal(account, old, new))
    pairs = {
        "old_as_new": (third, old),
        "new_as_old": (new, third),
        "old_as_old": (old, third),
        "new_as_new": (third, new),
    }
    contender = renewal(account, *pairs[shared_position])
    reads.assert_error(post(account, contender), 409, "reconciliation_required")
    assert commands.command_model().objects.count() == 1


def test_rm7_journal_cursor_invalidates_on_secondary_object_movement(renewal_setup):
    account, old, new = renewal_setup()
    spare = account.prescription()
    for _ in range(21):
        intent = commands.correction(account, spare)
        pending = commands.admit(account, intent)
        commands.assert_command(
            commands.execute(account, pending, intent), 200, "succeeded"
        )
    with freeze_time(timezone.now() + timedelta(seconds=1)):
        intent = renewal(account, old, new)
        pending = admit(account, intent)
    page = account.client.get(commands.COMMANDS).json()
    cursor = page["next_cursor"]
    assert cursor and len(page["results"]) == 20
    assert pending["command_id"] in {row["command_id"] for row in page["results"]}
    outsider = reads.ExpiryAccount()
    Prescription.objects.filter(pk=new.pk).update(patient_id=outsider.patient.pk)
    reads.assert_error(
        account.client.get(commands.COMMANDS, {"cursor": cursor}),
        409,
        "pagination_invalidated",
    )
    assert pending["command_id"] not in {
        row["command_id"]
        for row in account.client.get(commands.COMMANDS).json()["results"]
    }


def test_rm7_terminal_replay_proves_original_event_after_later_source_change(
    renewal_setup,
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    pending = admit(account, intent)
    terminal = assert_command(execute(account, pending, intent), 200, "succeeded")
    make_source(account, new, number=2)
    assert (
        account.detail(new).json()["item"]["revision"]
        != intent["expected_new_revision"]
    )
    before = snapshot(account, old, new)
    assert (
        assert_command(execute(account, pending, intent), 200, "succeeded") == terminal
    )
    assert snapshot(account, old, new) == before
    assert relation_model().objects.count() == 1


def test_rm2_legacy_photo_is_available_document_version_one(
    renewal_settings, command_setup
):
    account, old = command_setup()
    key = default_storage.save(f"synthetic/legacy-{uuid.uuid4()}.png", ContentFile(PNG))
    new = account.prescription(photo_prescription=key)
    intent = renewal(account, old, new)
    assert intent["reviewed_document_version"] == 1
    assert_command(execute(account, admit(account, intent), intent), 200, "succeeded")


def test_rm2_admission_kind_and_intent_cannot_be_cross_wired(renewal_setup):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    correction = commands.correction(account, old)
    for body in (
        {"kind": "date_correction", "intent": intent},
        {"kind": "renewal_confirmation", "intent": correction},
        {
            "kind": "renewal_confirmation",
            "intent": intent,
            "command_id": str(uuid.uuid4()),
        },
        {"kind": "renewal_confirmation", "intent": intent, "user_id": account.user.pk},
        {"kind": "renewal_confirmation", "intent": intent, "state": "succeeded"},
    ):
        reads.assert_error(
            account.client.post(commands.COMMANDS, body, format="json"),
            400,
            "invalid_request",
        )
    assert not commands.command_model().objects.exists()


def test_rm5_confirmation_and_commit_callbacks_never_emit_a_message(
    renewal_setup, monkeypatch, django_capture_on_commit_callbacks
):
    import socket

    from django.core import mail

    account, old, new = renewal_setup()
    attempted = []

    def forbid_network(*args, **kwargs):
        attempted.append("network")
        raise AssertionError("Renewal must not submit a message")

    def forbid_mail(*args, **kwargs):
        attempted.append("mail")
        raise AssertionError("Renewal must not submit mail")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setattr(mail, "send_mail", forbid_mail)
    with django_capture_on_commit_callbacks(execute=True):
        confirm(account, old, new)
        account.page()
    assert attempted == []


@pytest.mark.parametrize("point", ["relation", "terminal"])
def test_rm1_expiration_after_durable_write_rolls_back_authorized_effect(
    renewal_setup, point
):
    account, old, new = renewal_setup()
    intent = renewal(account, old, new)
    pending = admit(account, intent)
    token = AuthToken.objects.get(session_id=account.pair["session_id"], kind="access")
    before = snapshot(account, old, new)
    table = relation_model()._meta.db_table.upper()
    hit = []
    with freeze_time(timezone.now()) as clock:

        def expire_after_write(execute_sql, sql, params, many, context):
            result = execute_sql(sql, params, many, context)
            statement = sql.lstrip().upper()
            matches = {
                "relation": statement.startswith(f'INSERT INTO "{table}"'),
                "terminal": statement.startswith('UPDATE "NURSE_EXPIRYCOMMAND" ')
                and "succeeded" in (params or ()),
            }
            if not hit and matches[point]:
                hit.append(point)
                clock.move_to(token.expires_at + timedelta(seconds=1))
            return result

        with connection.execute_wrapper(expire_after_write):
            response = execute(account, pending, intent)
        assert hit == [point]
        reads.assert_error(response, 401, "authentication_required")
    assert snapshot(account, old, new) == before
    assert not relation_model().objects.exists()
    row = commands.command_model().objects.get(pk=pending["command_id"])
    assert row.state == "pending" and row.terminal_at is None and row.payload_ciphertext
