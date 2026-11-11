"""Internal C12 admission only. No transport, retry or delivery API."""

import uuid

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import APIException

from main.session_services import fingerprint
from nurse.expiry_calculation import CALCULATION_VERSION
from nurse.expiry_command_models import ExpiryFutureAuthority, ExpiryRenewal
from nurse.expiry_commands import active_context, private_expiry_commands
from nurse.expiry_reminder_calculation import (
    opportunity_state,
    reminder_milestones,
    reminder_window,
)
from nurse.expiry_reminder_guards import acquire_reminder_authority
from nurse.expiry_reminder_models import (
    ExpiryReminderAuthority,
    ExpiryReminderEvent,
    ExpiryReminderEventItem,
    ExpiryReminderEventRoute,
    ExpiryReminderOpportunity,
    ExpiryReminderPreference,
)
from nurse.expiry_state import ExpiryError, instant
from nurse.models import Nurse, Prescription
from nurse.push_interfaces import SelectedRoute, unambiguous_routes
from nurse.push_models import PushBinding, PushRegistryState


def runtime():
    try:
        raw = getattr(settings, "EXPIRY_REMINDER_NAMESPACE", "")
        namespace = uuid.UUID(raw)
        if str(namespace) != raw or namespace.version != 4:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ExpiryError("service_unavailable") from None
    rows = list(ExpiryReminderAuthority.objects.all())
    if (
        getattr(settings, "EXPIRY_REMINDERS_ENABLED", False) is not True
        or len(rows) != 1
        or rows[0].pk != 1
        or not rows[0].accepting
        or rows[0].namespace != namespace
    ):
        raise ExpiryError("service_unavailable")
    return namespace


def context(request):
    acquire_reminder_authority()
    namespace = runtime()
    try:
        if not request.user.is_authenticated or not isinstance(request.auth, tuple):
            raise ExpiryError("authentication_required")
        user, session, state = active_context(request)
    except APIException:
        raise ExpiryError("authentication_required") from None
    nurse = Nurse.objects.select_for_update().filter(user_id=user.pk).first()
    if nurse is None:
        raise ExpiryError("not_found")
    return user, session, state, nurse, namespace


def preference_for(user, state):
    row, _ = ExpiryReminderPreference.objects.get_or_create(
        user_id=user.pk, defaults={"principal_incarnation": state.incarnation}
    )
    if row.principal_incarnation != state.incarnation:
        raise ExpiryError("service_unavailable")
    return row


def preference_data(row):
    return {"lead_days": row.lead_days, "revision": str(row.revision)}


@private_expiry_commands()
def get_preference(request):
    try:
        with transaction.atomic():
            user, _, state, _, _ = context(request)
            return preference_data(preference_for(user, state))
    except DatabaseError:
        raise ExpiryError("service_unavailable") from None


@private_expiry_commands()
def update_preference(request, *, expected_revision, lead_days):
    if type(lead_days) is not int or lead_days not in (7, 8, 9, 10):
        raise ExpiryError("invalid_request")
    try:
        with transaction.atomic():
            user, _, state, _, _ = context(request)
            row = preference_for(user, state)
            if str(row.revision) != expected_revision:
                raise ExpiryError("revision_conflict")
            if row.lead_days == lead_days:
                return preference_data(row)
            row.lead_days, row.revision = lead_days, uuid.uuid4()
            row.save(update_fields=("lead_days", "revision"))
            active_context(request)
            return preference_data(row)
    except DatabaseError:
        raise ExpiryError("service_unavailable") from None


def route_selection(selected):
    try:
        return tuple(selected)
    except TypeError:
        raise ExpiryError("invalid_request") from None


def qualified_routes(selected, user, now):
    if any(
        not isinstance(route, SelectedRoute)
        or not isinstance(route.device_ref, str)
        or not 1 <= len(route.device_ref) <= 255
        or not isinstance(route.namespace, str)
        or not 1 <= len(route.namespace) <= 120
        or type(route.epoch) is not int
        or type(route.generation) is not int
        or route.epoch < 0
        or route.generation < 0
        for route in selected
    ):
        raise ExpiryError("invalid_request")
    registry = PushRegistryState.objects.filter(pk=1, accepting=True).first()
    if registry is None:
        return []
    qualified = []
    seen_bindings = set()
    for route in unambiguous_routes(selected):
        if route.proven is not True:
            continue
        try:
            binding_id = uuid.UUID(str(route.binding_id))
        except (ValueError, TypeError, AttributeError):
            continue
        if binding_id in seen_bindings:
            raise ExpiryError("invalid_request")
        seen_bindings.add(binding_id)
        binding = PushBinding.objects.filter(
            pk=binding_id,
            owner=user,
            owner__is_active=True,
            destination__namespace=route.namespace,
            epoch=route.epoch,
            generation=route.generation,
            restore_epoch=registry.restore_epoch,
            revoked_at__isnull=True,
            expires_at__gt=now,
            session__user=user,
            session__revoked_at__isnull=True,
            session__expires_at__gt=now,
            session__password_fingerprint=fingerprint(user),
            destination__epoch=F("epoch"),
            destination__generation=F("generation"),
        ).first()
        if binding is not None:
            qualified.append((route, binding.restore_epoch))
    return qualified


def event_data(event):
    items = list(
        ExpiryReminderEventItem.objects.filter(event=event)
        .select_related("opportunity")
        .order_by("pk")
    )
    state = event.state
    if state == "reserved" and timezone.now() >= event.deadline_at:
        state = "expired"
    return {
        "state": state,
        "event_id": str(event.pk),
        "service_day": event.service_day.isoformat(),
        "deadline_at": instant(event.deadline_at),
        "opportunity_ids": [str(item.opportunity_id) for item in items],
        "opportunities": [
            {
                "opportunity_id": str(item.opportunity_id),
                "state": item.opportunity.state,
            }
            for item in items
        ],
        "routes": [
            {
                "device_ref": row.device_ref,
                "namespace": row.namespace,
                "binding_id": str(row.binding_id),
                "epoch": row.epoch,
                "generation": row.generation,
                "state": row.state,
            }
            for row in ExpiryReminderEventRoute.objects.filter(event=event).order_by(
                "pk"
            )
        ],
    }


def eligible_opportunities(user, state, nurse, pref, now):
    day = reminder_window(now)["service_day"]
    renewed = ExpiryRenewal.objects.values_list("old_prescription_id", flat=True)
    rows = (
        Prescription.objects.filter(patient__nurse=nurse)
        .exclude(pk__in=renewed)
        .order_by("pk")
    )
    opportunities = []
    for rx in rows:
        if (
            rx.start_date is None
            or rx.end_date is None
            or not rx.start_date <= day <= rx.end_date
        ):
            continue
        future = ExpiryFutureAuthority.objects.filter(prescription_id=rx.pk).first()
        if future is None:
            raise ExpiryError("service_unavailable")
        for milestone in reminder_milestones(
            rx.start_date, rx.end_date, pref.lead_days
        ):
            if opportunity_state(milestone["eligible_date"], now) != "eligible":
                continue
            opportunity, _ = ExpiryReminderOpportunity.objects.get_or_create(
                user_id=user.pk,
                prescription_id=rx.pk,
                end_date=rx.end_date,
                kind=milestone["kind"],
                defaults={
                    "principal_incarnation": state.incarnation,
                    "due_date": milestone["due_date"],
                    "eligible_date": day,
                    "future_revision": future.revision,
                    "future_generation": future.generation,
                    "preference_revision": pref.revision,
                    "calculation_version": CALCULATION_VERSION,
                },
            )
            if (
                opportunity.principal_incarnation != state.incarnation
                or opportunity.admitted_at is not None
            ):
                continue
            # Only an unconsumed identity may be refreshed after invalidation.
            opportunity.due_date = milestone["due_date"]
            opportunity.eligible_date = day
            opportunity.future_revision, opportunity.future_generation = (
                future.revision,
                future.generation,
            )
            opportunity.preference_revision = pref.revision
            opportunity.calculation_version = CALCULATION_VERSION
            opportunity.state = "planned"
            opportunity.save()
            opportunities.append(opportunity)
    return opportunities


@private_expiry_commands()
def reserve_day(request, *, expected_preference_revision, selected_routes):
    try:
        with transaction.atomic():
            user, _, state, nurse, namespace = context(request)
            selected = route_selection(selected_routes)
            now = timezone.now()
            window = reminder_window(now)
            existing = ExpiryReminderEvent.objects.filter(
                user_id=user.pk, service_day=window["service_day"]
            ).first()
            if existing is not None:
                if (
                    existing.principal_incarnation != state.incarnation
                    or existing.namespace != namespace
                ):
                    raise ExpiryError("service_unavailable")
                return event_data(existing)
            # Expire only persisted, unconsumed plans; never synthesize backlog.
            ExpiryReminderOpportunity.objects.filter(
                user_id=user.pk,
                state="planned",
                admitted_at__isnull=True,
                eligible_date__lt=window["service_day"],
            ).update(state="missed")
            if opportunity_state(window["service_day"], now) == "missed":
                ExpiryReminderOpportunity.objects.filter(
                    user_id=user.pk,
                    state="planned",
                    admitted_at__isnull=True,
                    eligible_date=window["service_day"],
                ).update(state="missed")
            absent = {"event_id": None}
            if not window["is_open"]:
                return {"state": "outside_window", **absent}
            pref = preference_for(user, state)
            if str(pref.revision) != expected_preference_revision:
                raise ExpiryError("revision_conflict")
            opportunities = eligible_opportunities(user, state, nurse, pref, now)
            if not opportunities:
                return {"state": "no_opportunity", **absent}
            routes = qualified_routes(selected, user, now)
            if not routes:
                return {"state": "no_routes", **absent}
            event = ExpiryReminderEvent.objects.create(
                user_id=user.pk,
                principal_incarnation=state.incarnation,
                namespace=namespace,
                service_day=window["service_day"],
                deadline_at=window["deadline_at"],
                admitted_at=now,
            )
            for opportunity in opportunities:
                opportunity.state, opportunity.admitted_at = "admitted", now
                opportunity.save(update_fields=("state", "admitted_at"))
                ExpiryReminderEventItem.objects.create(
                    event=event, opportunity=opportunity
                )
            for route, restore_epoch in routes:
                ExpiryReminderEventRoute.objects.create(
                    event=event,
                    device_ref=route.device_ref,
                    namespace=route.namespace,
                    binding_id=route.binding_id,
                    epoch=route.epoch,
                    generation=route.generation,
                    restore_epoch=restore_epoch,
                )
            _, _, final_state = active_context(request)
            finished = timezone.now()
            if (
                qualified_routes(tuple(route for route, _ in routes), user, finished)
                != routes
            ):
                raise ExpiryError("service_unavailable")
            if (
                final_state.incarnation != state.incarnation
                or final_state.scope_revision != state.scope_revision
                or not reminder_window(timezone.now())["is_open"]
                or reminder_window(timezone.now())["service_day"]
                != window["service_day"]
            ):
                raise ExpiryError("service_unavailable")
            runtime()
            return event_data(event)
    except DatabaseError:
        raise ExpiryError("service_unavailable") from None
