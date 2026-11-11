"""Atomic correction admission, execution and reconciliation.

The common transaction authority precedes row locks. Every writer participates,
including B01/legacy SQL; admission never changes clinical dates.
"""

import hashlib
import uuid
from contextlib import contextmanager
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from rest_framework.authentication import get_authorization_header

from main.models import AuthSession, AuthToken
from main.session_services import fingerprint
from main.session_tokens import parse_token, verify_secret
from nurse.expiry_command_crypto import (
    allocate_command_id,
    canonical,
    matches,
    seal,
    unseal,
)
from nurse.expiry_command_guards import acquire_authority
from nurse.expiry_command_models import (
    ExpiryCommand,
    ExpiryCommandAuthority,
    ExpiryCommandJournal,
    ExpiryCorrectionAudit,
    ExpiryFutureAuthority,
)
from nurse.expiry_command_runtime import external_namespace, runtime
from nurse.expiry_command_serializers import ExpiryCorrectionSerializer
from nurse.expiry_models import ExpiryAccountState, ExpiryPrescriptionState
from nurse.expiry_state import (
    ExpiryError,
    authority,
    instant,
    read_context,
)
from nurse.models import Nurse, Patient, Prescription

User = get_user_model()


@contextmanager
def private_expiry_commands():
    from nurse.read_privacy import _read_route, install_log_redaction

    install_log_redaction()
    marker = _read_route.set("/api/v4/expiry/private/")
    try:
        yield
    finally:
        _read_route.reset(marker)


def command_data(row):
    return {
        "command_id": str(row.command_id),
        "mutation_epoch": str(row.mutation_epoch),
        "kind": row.kind,
        "state": row.state,
        "recorded_at": instant(row.recorded_at),
        "result": row.result,
        "error": row.error,
    }


def touch_journal(user_id, incarnation):
    ExpiryCommandJournal.objects.update_or_create(
        user_id=user_id,
        defaults={"principal_incarnation": incarnation, "revision": uuid.uuid4()},
    )


def journal_revision(user_id, state):
    row = (
        ExpiryCommandJournal.objects.filter(
            user_id=user_id, principal_incarnation=state[0]
        )
        .values_list("revision", flat=True)
        .first()
    )
    return hashlib.sha256(canonical([str(row), *state])).hexdigest()


def active_context(request):
    """Reread the presented secret and all B01 authority under the shared lock."""
    user = User.objects.select_for_update().filter(pk=request.user.pk).first()
    session_id = request.auth[0].pk
    session = AuthSession.objects.select_for_update().filter(pk=session_id).first()
    token = AuthToken.objects.select_for_update().filter(pk=request.auth[1].pk).first()
    try:
        raw = get_authorization_header(request).split()[1].decode("ascii")
        parsed = parse_token(raw)
    except (IndexError, UnicodeError):
        parsed = None
    now = timezone.now()
    if (
        user is None
        or session is None
        or token is None
        or parsed is None
        or parsed[0] != token.pk
        or token.kind != "access"
        or token.session_id != session.pk
        or session.user_id != user.pk
        or not verify_secret(parsed[1], token.token_hash)
        or not user.is_active
        or session.revoked_at
        or token.revoked_at
        or session.expires_at <= now
        or token.expires_at <= now
        or session.password_fingerprint != fingerprint(user)
    ):
        raise ExpiryError("authentication_required")
    state = (
        ExpiryAccountState.objects.select_for_update().filter(user_id=user.pk).first()
    )
    if state is None:
        raise ExpiryError("service_unavailable")
    return user, session, state


def lock_object(user_id, patient_id, prescription_id):
    nurse = Nurse.objects.select_for_update().filter(user_id=user_id).first()
    patient = Patient.objects.select_for_update().filter(pk=patient_id).first()
    if (
        nurse is None
        or patient is None
        or not nurse.patients.filter(pk=patient_id).exists()
    ):
        raise ExpiryError("not_found")
    row = (
        Prescription.objects.select_for_update()
        .filter(pk=prescription_id, patient_id=patient_id)
        .first()
    )
    if row is None:
        raise ExpiryError("not_found")
    return row


def accessible_commands(user_id, incarnation):
    nurse = Nurse.objects.filter(user_id=user_id).first()
    if nurse is None:
        raise ExpiryError("not_found")
    allowed = Prescription.objects.filter(
        pk=OuterRef("prescription_id"),
        patient_id=OuterRef("patient_id"),
        patient__nurse=nurse,
    )
    return ExpiryCommand.objects.filter(
        Exists(allowed),
        user_id=user_id,
        principal_incarnation=incarnation,
    )


def lookup_command(user_id, incarnation, command_id, lock=False):
    try:
        identifier = uuid.UUID(command_id)
        if str(identifier) != command_id or identifier.version != 4:
            raise ValueError
    except (TypeError, ValueError):
        raise ExpiryError("not_found") from None
    rows = ExpiryCommand.objects.filter(
        pk=identifier, user_id=user_id, principal_incarnation=incarnation
    )
    row = (rows.select_for_update() if lock else rows).first()
    if row is None:
        raise ExpiryError("not_found")
    return row


@transaction.atomic
def admit_command(request, intent):
    acquire_authority()
    namespace, version = runtime()
    user, session, state = active_context(request)
    lock_object(user.pk, intent["patient_id"], intent["prescription_id"])
    if intent["mutation_epoch"] != str(state.mutation_epoch):
        raise ExpiryError("command_epoch_closed")
    if ExpiryCommand.objects.filter(
        prescription_id=intent["prescription_id"], state="pending"
    ).exists():
        raise ExpiryError("reconciliation_required")
    row = ExpiryCommand(
        command_id=allocate_command_id(
            namespace, state.incarnation, state.mutation_epoch, version
        ),
        user_id=user.pk,
        principal_incarnation=state.incarnation,
        namespace=namespace,
        origin_session_id=session.pk,
        mutation_epoch=state.mutation_epoch,
        kind="date_correction",
        key_version=version,
        patient_id=intent["patient_id"],
        prescription_id=intent["prescription_id"],
    )
    seal(row, intent)
    active_context(request)
    row.save(force_insert=True)
    touch_journal(user.pk, state.incarnation)
    return command_data(row)


def terminal(row, *, result=None, error=None):
    row.state = "succeeded" if result is not None else "rejected"
    row.result = result
    row.error = ExpiryError(error).body if error else None
    row.payload_ciphertext = None
    row.terminal_at = timezone.now()
    row.save(
        update_fields=("state", "result", "error", "payload_ciphertext", "terminal_at")
    )
    touch_journal(row.user_id, row.principal_incarnation)
    return command_data(row)


def authorized_terminal(request, row, **outcome):
    data = terminal(row, **outcome)
    # Row locks stop revocations; they do not stop token/session time advancing.
    runtime()
    active_context(request)
    return data


@transaction.atomic
def execute_command(request, command_id, intent):
    acquire_authority()
    runtime()
    user, session, state = active_context(request)
    # Lookup never admits, and access to stored objects precedes intent diagnostics.
    row = lookup_command(user.pk, state.incarnation, command_id)
    prescription = lock_object(user.pk, row.patient_id, row.prescription_id)
    row = lookup_command(user.pk, state.incarnation, command_id, lock=True)
    if row.kind != "date_correction" or not matches(row, intent):
        raise ExpiryError("command_conflict")
    if row.state != "pending":
        active_context(request)
        return command_data(row)
    admitted = unseal(row)
    schema = ExpiryCorrectionSerializer(data=admitted)
    if (
        not schema.is_valid()
        or schema.validated_data != admitted
        or admitted["patient_id"] != row.patient_id
        or admitted["prescription_id"] != row.prescription_id
        or admitted["mutation_epoch"] != str(row.mutation_epoch)
    ):
        raise ExpiryError("service_unavailable")
    if (
        row.namespace != external_namespace()
        or row.mutation_epoch != state.mutation_epoch
    ):
        raise ExpiryError("command_epoch_closed")
    if session.pk != row.origin_session_id:
        raise ExpiryError("reconciliation_required")
    current = (
        ExpiryPrescriptionState.objects.select_for_update()
        .filter(prescription_id=prescription.pk)
        .first()
    )
    if current is None:
        raise ExpiryError("service_unavailable")
    active_context(request)
    if str(current.revision) != admitted["expected_revision"]:
        return authorized_terminal(request, row, error="revision_conflict")
    start, end = admitted["start_date"], admitted["end_date"]
    if start is not None and end is not None and start > end:
        return authorized_terminal(request, row, error="invalid_dates")
    before = (prescription.start_date, prescription.end_date)
    Prescription.objects.filter(pk=prescription.pk).update(
        start_date=start, end_date=end
    )
    current.refresh_from_db()
    future = ExpiryFutureAuthority.objects.filter(
        prescription_id=prescription.pk
    ).first()
    if (
        future is None
        or future.revision != current.revision
        or current.revision == uuid.UUID(admitted["expected_revision"])
    ):
        raise ExpiryError("service_unavailable")
    ExpiryCorrectionAudit.objects.create(
        command_id=row.command_id,
        user_id=user.pk,
        principal_incarnation=state.incarnation,
        origin_session_id=session.pk,
        patient_id=row.patient_id,
        prescription_id=row.prescription_id,
        before_revision=admitted["expected_revision"],
        after_revision=current.revision,
        before_start_date=before[0],
        before_end_date=before[1],
        after_start_date=date.fromisoformat(start) if start else None,
        after_end_date=date.fromisoformat(end) if end else None,
    )
    context = read_context(timezone.now(), authority(user.pk))
    active_context(request)
    runtime()
    return authorized_terminal(
        request,
        row,
        result={
            "patient_id": row.patient_id,
            "prescription_id": row.prescription_id,
            "revision": str(current.revision),
            "replacement_prescription_id": None,
            "collection_revision": context["collection_revision"],
        },
    )


def lock_abandoned(row):
    # The same authority and lock order as execution. No TTL proves an outcome.
    User.objects.select_for_update().filter(pk=row.user_id).first()
    AuthSession.objects.select_for_update().filter(pk=row.origin_session_id).first()
    list(
        AuthToken.objects.select_for_update()
        .filter(session_id=row.origin_session_id)
        .order_by("pk")
    )
    Nurse.objects.select_for_update().filter(user_id=row.user_id).first()
    Patient.objects.select_for_update().filter(pk=row.patient_id).first()
    Prescription.objects.select_for_update().filter(pk=row.prescription_id).first()
    locked = ExpiryCommand.objects.select_for_update().get(pk=row.pk)
    if locked.state == "pending":
        terminal(locked, error="command_abandoned")


def close_account(user_id):
    state = (
        ExpiryAccountState.objects.select_for_update().filter(user_id=user_id).first()
    )
    if state is not None:
        state.mutation_epoch = uuid.uuid4()
        state.save(update_fields=("mutation_epoch",))
        touch_journal(user_id, state.incarnation)
    rows = ExpiryCommand.objects.filter(user_id=user_id, state="pending").order_by(
        "command_id"
    )
    for row in rows:
        lock_abandoned(row)


@transaction.atomic
def fence(request):
    acquire_authority()
    runtime()
    user, session, state = active_context(request)
    # A Nurse is required even when the currently accessible journal is empty.
    if not Nurse.objects.select_for_update().filter(user_id=user.pk).exists():
        raise ExpiryError("not_found")
    close_account(user.pk)
    active_context(request)
    current = authority(user.pk)
    return {
        "contract_revision": "0.2-decision",
        "mutation_epoch": current[2],
        "journal_revision": journal_revision(user.pk, current),
    }


@private_expiry_commands()
@transaction.atomic
def purge_commands():
    acquire_authority()
    cutoff = timezone.now() - timedelta(days=30)
    expired = ExpiryCommand.objects.filter(terminal_at__lte=cutoff)
    users = sorted(set(expired.values_list("user_id", flat=True)))
    # Also resolve previously closed epochs; never reap by age alone.
    states = dict(ExpiryAccountState.objects.values_list("user_id", "mutation_epoch"))
    closed_users = {
        row.user_id
        for row in ExpiryCommand.objects.filter(state="pending")
        if row.mutation_epoch != states.get(row.user_id)
    }
    for user_id in sorted(set(users) | closed_users):
        User.objects.select_for_update().filter(pk=user_id).first()
        close_account(user_id)
    expired.delete()


@private_expiry_commands()
@transaction.atomic
def close_restored_commands():
    namespace = external_namespace()
    acquire_authority()
    row = ExpiryCommandAuthority.objects.select_for_update().filter(pk=1).first()
    if row is None or row.namespace == namespace:
        raise ExpiryError("service_unavailable")
    row.accepting = False
    row.namespace = namespace
    row.save(update_fields=("accepting", "namespace"))
    users = set(ExpiryAccountState.objects.values_list("user_id", flat=True))
    users.update(
        ExpiryCommand.objects.filter(state="pending").values_list("user_id", flat=True)
    )
    for user_id in sorted(users):
        User.objects.select_for_update().filter(pk=user_id).first()
        close_account(user_id)
