"""#283 AC1–3: real B01/B07 setup, durable admission, never provider submission."""

import uuid
from dataclasses import replace
from datetime import date
from importlib import import_module

import pytest
from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from freezegun import freeze_time
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from main.models import AuthSession
from main.session_authentication import OpaqueSessionAuthentication
from nurse.expiry_state import ExpiryError
from nurse.models import Prescription
from nurse.push_interfaces import SelectedRoute, summarize_results
from nurse.push_models import PushBinding, PushRegistryState
from tests.nurse import test_expiry_reads_v4 as reads
from tests.nurse.test_push_registry import Harness

pytestmark = pytest.mark.django_db
expiry_settings = reads.expiry_settings
NAMESPACE = "6d02a662-a7a5-45a0-b68d-c898a06f6c18"


def services():
    try:
        return import_module("nurse.expiry_reminder_services")
    except ModuleNotFoundError as exc:
        if exc.name != "nurse.expiry_reminder_services":
            raise
        pytest.fail("#283 requires the additive reminder reservation services")


def model(name):
    try:
        return apps.get_model("nurse", "ExpiryReminder" + name)
    except LookupError:
        pytest.fail(f"#283 requires additive nurse.ExpiryReminder{name}")


def request(account):
    return Request(
        APIRequestFactory().get(
            "/internal/reminder-reservation/",
            HTTP_AUTHORIZATION=f"Bearer {account.pair['access_token']}",
        ),
        authenticators=[OpaqueSessionAuthentication()],
    )


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(ExpiryError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def open_runtime():
    return model("Authority").objects.update_or_create(
        pk=1, defaults={"namespace": NAMESPACE, "accepting": True}
    )[0]


def proven_route(harness, account, token=None):
    # Resolve the token through the B01 authenticator, never infer it from its text.
    authenticated = request(account)
    authenticated.user
    access = authenticated.auth[1]
    auth = (access.session_id, access.pk)
    client, _, _ = harness.enroll(auth, token=token or f"synthetic-283-{uuid.uuid4()}")
    result, status = harness.confirm(auth, client)
    assert status == 201
    return SelectedRoute(
        device_ref=f"qualified-device-{uuid.uuid4()}",
        namespace="synthetic.android.dev",
        binding_id=result["binding_id"],
        epoch=result["epoch"],
        generation=result["generation"],
        proven=True,
    )


@pytest.fixture
def reminder_setup(expiry_settings, settings):
    settings.EXPIRY_REMINDERS_ENABLED = True
    settings.EXPIRY_REMINDER_NAMESPACE = NAMESPACE

    def create():
        open_runtime()
        account = reads.ExpiryAccount()
        rx = account.prescription(
            start_date=date(2026, 3, 1), end_date=date(2026, 4, 7)
        )
        harness = Harness()
        route = proven_route(harness, account)
        preference = services().get_preference(request(account))
        return account, rx, harness, route, preference

    with freeze_time("2026-03-28T12:00:00Z"):
        yield create


def reserve(account, preference, routes):
    return services().reserve_day(
        request(account),
        expected_preference_revision=preference["revision"],
        selected_routes=routes,
    )


def assert_reserved(result, count=1):
    assert result["state"] == "reserved"
    uuid.UUID(result["event_id"])
    assert result["service_day"] == "2026-03-28"
    assert result["deadline_at"] == "2026-03-28T17:00:00Z"
    assert len(result["opportunity_ids"]) == count
    assert len(set(result["opportunity_ids"])) == count
    for identifier in result["opportunity_ids"]:
        uuid.UUID(identifier)
    assert set(result) == {
        "state",
        "event_id",
        "service_day",
        "deadline_at",
        "opportunity_ids",
        "opportunities",
        "routes",
    }

    assert result["opportunities"] == [
        {"opportunity_id": identifier, "state": "admitted"}
        for identifier in result["opportunity_ids"]
    ]
    assert all(route["state"] == "reserved" for route in result["routes"])


def assert_same_admission(current, initial):
    for key in ("event_id", "service_day", "deadline_at", "opportunity_ids"):
        assert current[key] == initial[key]
    assert [row["opportunity_id"] for row in current["opportunities"]] == initial[
        "opportunity_ids"
    ]
    assert [
        {key: value for key, value in row.items() if key != "state"}
        for row in current["routes"]
    ] == [
        {key: value for key, value in row.items() if key != "state"}
        for row in initial["routes"]
    ]


def assert_cancelled_replay(current, initial):
    assert_same_admission(current, initial)
    assert current["state"] == "cancelled"
    assert all(row["state"] == "cancelled" for row in current["opportunities"])


def test_ac1_default_preference_is_durable_and_cas_changes_its_revision(reminder_setup):
    account, _, _, _, preference = reminder_setup()
    assert preference["lead_days"] == 10
    uuid.UUID(preference["revision"])
    assert services().get_preference(request(account)) == preference
    changed = services().update_preference(
        request(account), expected_revision=preference["revision"], lead_days=7
    )
    assert changed["lead_days"] == 7 and changed["revision"] != preference["revision"]
    assert_error(
        "revision_conflict",
        services().update_preference,
        request(account),
        expected_revision=preference["revision"],
        lead_days=9,
    )
    assert services().get_preference(request(account)) == changed


@pytest.mark.parametrize("invalid", [True, "7", 6, 11, None])
def test_ac1_preference_accepts_only_strict_integer_7_through_10(
    reminder_setup, invalid
):
    account, _, _, _, preference = reminder_setup()
    assert_error(
        "invalid_request",
        services().update_preference,
        request(account),
        expected_revision=preference["revision"],
        lead_days=invalid,
    )
    assert services().get_preference(request(account)) == preference


def test_ac2_one_idel_day_freezes_first_group_and_has_no_clinical_or_delivery_effect(
    reminder_setup, monkeypatch
):
    account, rx, harness, route, pref = reminder_setup()
    other_rx = account.prescription(end_date=date(2026, 4, 7))
    before = list(Prescription.objects.order_by("pk").values())
    deliveries = len(harness.control.deliveries)
    monkeypatch.setattr(
        harness.control,
        "deliver",
        lambda **kwargs: pytest.fail("reservation must never deliver"),
    )
    result = reserve(account, pref, [route])
    assert_reserved(result, 2)
    assert result["routes"] == [
        {
            **{
                k: getattr(route, k)
                for k in (
                    "device_ref",
                    "namespace",
                    "binding_id",
                    "epoch",
                    "generation",
                )
            },
            "state": "reserved",
        }
    ]
    account.prescription(end_date=date(2026, 4, 7))
    replay = reserve(account, {"revision": str(uuid.uuid4())}, [])
    assert replay == result  # Daily replay precedes preference CAS, after auth/runtime.
    assert model("Event").objects.count() == 1
    assert model("EventItem").objects.count() == 2
    assert set(
        model("Opportunity")
        .objects.filter(admitted_at__isnull=False)
        .values_list("prescription_id", flat=True)
    ) == {rx.pk, other_rx.pk}
    assert (
        list(
            Prescription.objects.filter(pk__in=[rx.pk, other_rx.pk])
            .order_by("pk")
            .values()
        )
        == before
    )
    assert len(harness.control.deliveries) == deliveries


@pytest.mark.parametrize("selection", ["empty", "unproven", "ambiguous"])
def test_ac2_no_qualified_route_never_admits_or_consumes_daily_quota(
    reminder_setup, selection
):
    account, _, _, route, pref = reminder_setup()
    choices = {
        "empty": [],
        "unproven": [replace(route, proven=False)],
        "ambiguous": [route, replace(route, namespace="synthetic.ios.dev")],
    }
    result = reserve(account, pref, choices[selection])
    assert result["state"] == "no_routes" and result["event_id"] is None
    assert not model("Event").objects.exists()
    assert not model("Opportunity").objects.filter(admitted_at__isnull=False).exists()
    assert_reserved(reserve(account, pref, [route]))


@pytest.mark.parametrize(
    "stale",
    ["epoch", "generation", "namespace", "owner", "session", "restore", "revoked"],
)
def test_ac2_qualified_port_is_revalidated_against_live_binding(reminder_setup, stale):
    account, _, _, route, pref = reminder_setup()
    binding = PushBinding.objects.get(pk=route.binding_id)
    updates = {
        "epoch": {"epoch": binding.epoch + 1},
        "generation": {"generation": binding.generation + 1},
        "namespace": {},
        "owner": {"owner_id": None},
        "session": {"session_id": None},
        "restore": {"restore_epoch": uuid.uuid4()},
        "revoked": {"revoked_at": timezone.now()},
    }
    if stale == "namespace":
        route = replace(route, namespace="synthetic.ios.dev")
    else:
        PushBinding.objects.filter(pk=binding.pk).update(**updates[stale])
    result = reserve(account, pref, [route])
    assert result["state"] == "no_routes" and result["event_id"] is None
    assert not model("Event").objects.exists()


def test_ac2_accounts_are_isolated_and_rotation_cannot_extend_admitted_snapshot(
    reminder_setup,
):
    account, _, harness, route, pref = reminder_setup()
    original = reserve(account, pref, [route])
    replacement = proven_route(harness, account)
    assert reserve(account, pref, [replacement]) == original
    other = reads.ExpiryAccount()
    other.prescription(end_date=date(2026, 4, 7))
    other_route = proven_route(harness, other)
    other_pref = services().get_preference(request(other))
    separate = reserve(other, other_pref, [other_route])
    assert separate["event_id"] != original["event_id"]
    assert model("Event").objects.count() == 2


def test_ac1_j1_only_today_no_backlog_and_no_admission_at_deadline(reminder_setup):
    account, rx, _, route, pref = reminder_setup()
    Prescription.objects.filter(pk=rx.pk).update(end_date=date(2026, 3, 29))
    result = reserve(account, pref, [route])
    assert_reserved(result)
    assert list(
        model("Opportunity")
        .objects.filter(pk__in=result["opportunity_ids"])
        .values_list("kind", flat=True)
    ) == ["j1"]
    with freeze_time("2026-03-29T16:00:00Z"):
        account.pair = reads.login(account.user.username, reads.PASSWORD)
        closed = reserve(account, pref, [route])
        assert closed["state"] == "outside_window" and closed["event_id"] is None
    assert model("Event").objects.count() == 1


def test_ac3_correction_invalidates_in_same_transaction_and_never_rearms_identity(
    reminder_setup,
):
    account, rx, _, route, pref = reminder_setup()
    result = reserve(account, pref, [route])
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE nurse_prescription SET end_date=%s WHERE id=%s",
                [date(2026, 4, 8), rx.pk],
            )
        row = model("Opportunity").objects.get(pk=result["opportunity_ids"][0])
        assert row.state == "cancelled" and row.admitted_at is not None
        transaction.set_rollback(True)
    assert model("Opportunity").objects.get(pk=row.pk).state == "admitted"
    Prescription.objects.filter(pk=rx.pk).update(end_date=date(2026, 4, 8))
    Prescription.objects.filter(pk=rx.pk).update(end_date=date(2026, 4, 7))
    replay = reserve(account, pref, [route])
    assert_cancelled_replay(replay, result)
    assert replay["routes"][0]["state"] == "cancelled"
    assert model("EventItem").objects.count() == 1


def test_ac3_preference_and_scope_changes_preserve_admission_history(reminder_setup):
    account, _, _, route, pref = reminder_setup()
    result = reserve(account, pref, [route])
    services().update_preference(
        request(account), expected_revision=pref["revision"], lead_days=7
    )
    account.nurse.patients.remove(account.patient)
    assert model("Event").objects.filter(pk=result["event_id"]).exists()
    assert (
        model("Opportunity").objects.get(pk=result["opportunity_ids"][0]).admitted_at
        is not None
    )
    assert (
        model("Opportunity").objects.get(pk=result["opportunity_ids"][0]).state
        == "cancelled"
    )
    assert model("Transition").objects.exists()


def test_ac3_session_revoked_after_initial_authentication_cannot_replay(reminder_setup):
    account, _, _, route, pref = reminder_setup()
    result = reserve(account, pref, [route])
    stale_request = request(account)
    assert stale_request.user.pk == account.user.pk
    AuthSession.objects.filter(pk=account.pair["session_id"]).update(
        revoked_at=timezone.now()
    )
    assert_error(
        "authentication_required",
        services().reserve_day,
        stale_request,
        expected_preference_revision=pref["revision"],
        selected_routes=[route],
    )
    assert model("Event").objects.filter(pk=result["event_id"]).exists()


def test_ac2_refusal_and_uncertainty_stay_distinct_without_new_http_surface():
    assert summarize_results(["refused", "uncertain"]) == {
        "counts": {"accepted": 0, "refused": 1, "uncertain": 1, "cancelled": 0},
        "partial": True,
    }
    paths = SchemaGenerator().get_schema(public=True)["paths"]
    assert not any("reminder" in path or "preference" in path for path in paths)
    api = services()
    assert not any(
        hasattr(api, name) for name in ("send", "retry", "fallback", "record_attempt")
    )


def test_ac3_incarnation_rotation_cannot_reset_consumed_daily_quota(reminder_setup):
    account, _, _, route, pref = reminder_setup()
    result = reserve(account, pref, [route])
    state = apps.get_model("nurse", "ExpiryAccountState")
    state.objects.filter(user_id=account.user.pk).update(incarnation=uuid.uuid4())
    try:
        replay = reserve(account, pref, [route])
    except ExpiryError as exc:
        assert exc.code == "service_unavailable"
    else:
        assert replay["state"] == "no_opportunity" and replay["event_id"] is None
    assert model("Event").objects.count() == 1
    assert model("Event").objects.filter(pk=result["event_id"]).exists()


def test_ac1_future_unknown_invalid_and_elapsed_milestones_do_not_form_backlog(
    reminder_setup,
):
    account, rx, _, route, pref = reminder_setup()
    Prescription.objects.filter(pk=rx.pk).update(start_date=date(2026, 4, 6))
    for fields in (
        {"start_date": None, "end_date": None},
        {"start_date": date(2026, 4, 8), "end_date": date(2026, 4, 7)},
        {"end_date": date(2026, 3, 28)},
        {"end_date": date(2026, 3, 27)},
    ):
        account.prescription(**fields)
    result = reserve(account, pref, [route])
    assert result["state"] == "no_opportunity" and result["event_id"] is None
    assert not model("Event").objects.exists()
    assert not model("Opportunity").objects.filter(admitted_at__isnull=False).exists()


def test_ac2_refused_and_uncertain_snapshots_survive_replay_preference_and_invalidation(
    reminder_setup,
):
    account, rx, harness, route, pref = reminder_setup()
    other_route = proven_route(harness, account)
    result = reserve(account, pref, [route, other_route])
    routes = model("EventRoute")
    routes.objects.filter(
        event_id=result["event_id"], device_ref=route.device_ref
    ).update(state="refused")
    routes.objects.filter(
        event_id=result["event_id"], device_ref=other_route.device_ref
    ).update(state="uncertain")
    before = dict(
        routes.objects.filter(event_id=result["event_id"]).values_list(
            "device_ref", "state"
        )
    )
    services().update_preference(
        request(account), expected_revision=pref["revision"], lead_days=7
    )
    Prescription.objects.filter(pk=rx.pk).update(end_date=date(2026, 4, 8))
    replay = reserve(account, pref, [])
    assert_cancelled_replay(replay, result)
    assert (
        dict(
            routes.objects.filter(event_id=result["event_id"]).values_list(
                "device_ref", "state"
            )
        )
        == before
    )
    assert {row["device_ref"]: row["state"] for row in replay["routes"]} == before
    assert set(before.values()) == {"refused", "uncertain"}
    assert model("Event").objects.count() == 1


@pytest.mark.parametrize("change", ["revoked", "rotated"])
@pytest.mark.parametrize("prior_state", ["reserved", "refused", "uncertain"])
def test_ac2_one_binding_change_preserves_other_original_routes_and_daily_snapshot(
    reminder_setup, change, prior_state
):
    account, _, harness, route, pref = reminder_setup()
    other_route = proven_route(harness, account)
    result = reserve(account, pref, [route, other_route])
    routes = model("EventRoute")
    routes.objects.filter(
        event_id=result["event_id"], device_ref=route.device_ref
    ).update(state=prior_state)
    binding = PushBinding.objects.get(pk=route.binding_id)
    changes = {
        "revoked": {"revoked_at": timezone.now()},
        "rotated": {"generation": binding.generation + 1},
    }
    with transaction.atomic():
        PushBinding.objects.filter(pk=binding.pk).update(**changes[change])
        states = dict(
            routes.objects.filter(event_id=result["event_id"]).values_list(
                "device_ref", "state"
            )
        )
        assert states[route.device_ref] == (
            "cancelled" if prior_state == "reserved" else prior_state
        )
        assert states[other_route.device_ref] == "reserved"
        assert model("Event").objects.get(pk=result["event_id"]).state == "reserved"
        assert set(
            model("Opportunity")
            .objects.filter(pk__in=result["opportunity_ids"])
            .values_list("state", flat=True)
        ) == {"admitted"}
    replacement = proven_route(harness, account)
    replay = reserve(account, pref, [replacement])
    assert_same_admission(replay, result)
    assert replay["state"] == "reserved"
    assert {row["device_ref"]: row["state"] for row in replay["routes"]} == states
    assert routes.objects.filter(event_id=result["event_id"]).count() == 2


def test_ac2_calculation_version_is_frozen_in_admitted_opportunity(
    reminder_setup, monkeypatch
):
    account, rx, _, route, pref = reminder_setup()
    expected = import_module("nurse.expiry_calculation").CALCULATION_VERSION
    result = reserve(account, pref, [route])
    opportunity = model("Opportunity").objects.get(pk=result["opportunity_ids"][0])
    assert getattr(opportunity, "calculation_version", None) == expected
    monkeypatch.setattr(services(), "CALCULATION_VERSION", "synthetic-next-version")
    Prescription.objects.filter(pk=rx.pk).update(end_date=date(2026, 4, 8))
    replay = reserve(account, pref, [])
    assert_cancelled_replay(replay, result)
    opportunity.refresh_from_db()
    assert opportunity.state == "cancelled"
    assert opportunity.calculation_version == expected


def test_review_replay_at_deadline_reports_expired_and_keeps_admission(reminder_setup):
    account, _, _, route, pref = reminder_setup()
    result = reserve(account, pref, [route])
    with freeze_time("2026-03-28T17:00:00Z"):
        account.pair = reads.login(account.user.username, reads.PASSWORD)
        replay = reserve(account, pref, [])
    assert_same_admission(replay, result)
    assert replay["state"] == "expired"
    assert model("Event").objects.get(pk=result["event_id"]).state == "reserved"
    assert model("Event").objects.count() == 1


def test_review_same_preference_after_cas_does_not_write_or_cancel(reminder_setup):
    account, _, _, route, pref = reminder_setup()
    result = reserve(account, pref, [route])
    writes = []

    def watch(execute, sql, params, many, context):
        if sql.lstrip().upper().startswith('UPDATE "NURSE_EXPIRYREMINDERPREFERENCE"'):
            writes.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(watch):
        unchanged = services().update_preference(
            request(account), expected_revision=pref["revision"], lead_days=10
        )
    assert unchanged == pref
    assert writes == []
    assert reserve(account, pref, [route]) == result
    assert_error(
        "revision_conflict",
        services().update_preference,
        request(account),
        expected_revision=str(uuid.uuid4()),
        lead_days=10,
    )


@pytest.mark.parametrize("selection", [None, 17, True])
def test_review_non_iterable_selection_is_invalid_without_admission(
    reminder_setup, selection
):
    account, _, _, _, pref = reminder_setup()
    assert_error("invalid_request", reserve, account, pref, selection)
    assert not model("Event").objects.exists()
    assert not model("Opportunity").objects.exists()


def test_review_two_device_refs_cannot_reserve_one_binding_twice(reminder_setup):
    account, _, _, route, pref = reminder_setup()
    alias = replace(route, device_ref="another-qualified-reference")
    assert_error("invalid_request", reserve, account, pref, [route, alias])
    assert not model("Event").objects.exists()
    assert not model("Opportunity").objects.exists()


def test_review_unique_binding_and_event_state_are_database_constraints(reminder_setup):
    account, _, _, route, pref = reminder_setup()
    result = reserve(account, pref, [route])
    row = model("EventRoute").objects.get(event_id=result["event_id"])
    values = {
        field.name: getattr(row, field.name)
        for field in row._meta.fields
        if not field.primary_key
    }
    values["device_ref"] = "a-second-reference"
    with pytest.raises(IntegrityError), transaction.atomic():
        model("EventRoute").objects.create(**values)
    with pytest.raises(IntegrityError), transaction.atomic():
        model("Event").objects.filter(pk=result["event_id"]).update(state="sent")


def test_review_one_rx_correction_preserves_other_items_until_last_invalidation(
    reminder_setup,
):
    account, rx, _, route, pref = reminder_setup()
    other = account.prescription(end_date=rx.end_date)
    result = reserve(account, pref, [route])
    identifiers = dict(
        model("Opportunity").objects.values_list("prescription_id", "id")
    )
    with transaction.atomic():
        Prescription.objects.filter(pk=rx.pk).update(end_date=date(2026, 4, 8))
        replay = reserve(account, pref, [])
        assert_same_admission(replay, result)
        assert replay["state"] == "reserved"
        assert {
            row["opportunity_id"]: row["state"] for row in replay["opportunities"]
        } == {
            str(identifiers[rx.pk]): "cancelled",
            str(identifiers[other.pk]): "admitted",
        }
        assert replay["routes"][0]["state"] == "reserved"
    Prescription.objects.filter(pk=other.pk).update(end_date=date(2026, 4, 8))
    replay = reserve(account, pref, [])
    assert_cancelled_replay(replay, result)
    assert replay["routes"][0]["state"] == "cancelled"


def test_review_unrelated_event_is_not_rewritten_by_clinical_invalidation(
    reminder_setup,
):
    account, rx, harness, route, pref = reminder_setup()
    other = reads.ExpiryAccount()
    other.prescription(end_date=rx.end_date)
    other_route = proven_route(harness, other)
    other_pref = services().get_preference(request(other))
    first = reserve(account, pref, [route])
    second = reserve(other, other_pref, [other_route])
    model("Event").objects.filter(pk=second["event_id"]).update(state="cancelled")
    # A reserved historical route on another closed event is not this writer's scope.
    Prescription.objects.filter(pk=rx.pk).update(end_date=date(2026, 4, 8))
    assert_cancelled_replay(reserve(account, pref, []), first)
    assert (
        model("EventRoute").objects.get(event_id=second["event_id"]).state == "reserved"
    )


def test_review_registry_noop_preserves_original_routes_but_restore_cancels_them(
    reminder_setup,
):
    account, _, _, route, pref = reminder_setup()
    result = reserve(account, pref, [route])
    state = PushRegistryState.objects.get(pk=1)
    PushRegistryState.objects.filter(pk=1).update(
        accepting=state.accepting, restore_epoch=state.restore_epoch
    )
    assert reserve(account, pref, []) == result
    PushRegistryState.objects.filter(pk=1).update(restore_epoch=uuid.uuid4())
    replay = reserve(account, pref, [])
    assert_same_admission(replay, result)
    assert replay["state"] == "reserved"
    assert replay["routes"][0]["state"] == "cancelled"


def test_review_persisted_unadmitted_plan_becomes_missed_at_deadline(reminder_setup):
    account, _, _, _, pref = reminder_setup()
    assert reserve(account, pref, [])["state"] == "no_routes"
    with freeze_time("2026-03-28T17:00:00Z"):
        account.pair = reads.login(account.user.username, reads.PASSWORD)
        assert reserve(account, pref, [])["state"] == "outside_window"
    assert list(model("Opportunity").objects.values_list("state", flat=True)) == [
        "missed"
    ]
    assert not model("Event").objects.exists()
