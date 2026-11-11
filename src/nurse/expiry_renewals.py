"""Explicit two-object renewal; caller holds the shared command authority."""

import uuid

from django.db.models import Q
from django.utils import timezone

from nurse.expiry_command_models import ExpiryFutureAuthority, ExpiryRenewal
from nurse.expiry_models import ExpiryPrescriptionState
from nurse.expiry_state import ExpiryError, authority, read_context


def apply_renewal(command, objects, intent):
    # Import at execution to share the bounded source validator without a cycle.
    from nurse.expiry_views import authorized_rows, document_version

    current = {
        row.prescription_id: row
        for row in ExpiryPrescriptionState.objects.select_for_update()
        .filter(prescription_id__in=objects)
        .order_by("prescription_id")
    }
    if len(current) != 2:
        raise ExpiryError("service_unavailable")
    old_id, new_id = command.prescription_id, command.new_prescription_id
    if any(
        str(current[identifier].revision) != intent[f"expected_{name}_revision"]
        for identifier, name in ((old_id, "old"), (new_id, "new"))
    ):
        return {"error": "revision_conflict"}
    # An incoming link on old permits A→B→C. Any use of target or outgoing
    # link on old forbids a cycle, fork, merge or retroactive chain insertion.
    if ExpiryRenewal.objects.filter(
        Q(old_prescription_id__in=(old_id, new_id)) | Q(new_prescription_id=new_id)
    ).exists():
        return {"error": "state_conflict"}
    source = authorized_rows(command.user_id).filter(pk=new_id).first()
    if source is None:
        raise ExpiryError("not_found")
    version = document_version(source)
    if version is None or version != intent["reviewed_document_version"]:
        return {"error": "source_unavailable"}
    after = {identifier: uuid.uuid4() for identifier in (old_id, new_id)}
    ExpiryRenewal.objects.create(
        command_id=command.command_id,
        user_id=command.user_id,
        principal_incarnation=command.principal_incarnation,
        origin_session_id=command.origin_session_id,
        patient_id=command.patient_id,
        old_prescription_id=old_id,
        new_prescription_id=new_id,
        reviewed_document_version=version,
        patient_reviewed=intent["patient_reviewed"],
        pages_reviewed=intent["pages_reviewed"],
        dates_reviewed=intent["dates_reviewed"],
        replacement_confirmed=intent["replacement_confirmed"],
        before_old_revision=current[old_id].revision,
        before_new_revision=current[new_id].revision,
        after_old_revision=after[old_id],
        after_new_revision=after[new_id],
    )
    for identifier in (old_id, new_id):
        current[identifier].refresh_from_db()
        future = ExpiryFutureAuthority.objects.filter(
            prescription_id=identifier
        ).first()
        if (
            current[identifier].revision != after[identifier]
            or future is None
            or future.revision != after[identifier]
        ):
            raise ExpiryError("service_unavailable")
    context = read_context(timezone.now(), authority(command.user_id))
    return {
        "result": {
            "patient_id": command.patient_id,
            "prescription_id": old_id,
            "revision": str(after[old_id]),
            "replacement_prescription_id": new_id,
            "collection_revision": context["collection_revision"],
        }
    }
