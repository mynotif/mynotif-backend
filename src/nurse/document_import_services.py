"""Idempotency reservation and server-side validation services."""

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from nurse.document_import_schemas import ImportConflict, _error
from nurse.document_scanner import scan_bytes
from nurse.document_validation import (
    MAX_IMAGE_PIXELS,
    MAX_TOTAL_IMAGE_PIXELS,
    DocumentInvalid,
    validate_document,
)
from nurse.models import DocumentImport, DocumentImportState

TTL = timedelta(hours=24)
RECEIVE_LEASE = timedelta(seconds=120)


def _reserve(request, patient, import_uuid, fingerprint):
    now, lease = timezone.now(), uuid.uuid4()
    try:
        with transaction.atomic():
            operation = DocumentImport.objects.create(
                owner=request.user,
                patient=patient,
                import_uuid=import_uuid,
                state=DocumentImportState.RECEIVING,
                initial_fingerprint=fingerprint,
                lease_token=lease,
                lease_expires_at=now + RECEIVE_LEASE,
                expires_at=now + TTL,
            )
        return operation, lease, True
    except IntegrityError:
        with transaction.atomic():
            operation = DocumentImport.objects.select_for_update().get(
                owner=request.user, import_uuid=import_uuid
            )
            if (
                operation.patient_id != patient.pk
                or operation.initial_fingerprint != fingerprint
            ):
                raise ImportConflict({"code": "idempotency_conflict"})
            if operation.state in (
                DocumentImportState.CONFIRMED,
                DocumentImportState.REJECTED,
                DocumentImportState.ABANDONED,
                DocumentImportState.EXPIRED,
                DocumentImportState.READY,
            ):
                return operation, None, False
            if operation.lease_expires_at and operation.lease_expires_at > now:
                raise ImportConflict({"code": "operation_in_progress"})
            operation.lease_token = lease
            operation.lease_expires_at = now + RECEIVE_LEASE
            operation.state = DocumentImportState.RECEIVING
            operation.save(
                update_fields=("lease_token", "lease_expires_at", "state", "updated_at")
            )
            return operation, lease, True


def _validate_files(files, metadata):
    inspected, total_pixels = [], 0
    for content in files:
        scan_bytes(
            content,
            getattr(settings, "DOCUMENT_CLAMAV_SOCKET", "/run/clamav/clamd.ctl"),
        )
        details = validate_document(content)
        inspected.append(details)
        if details["mime_type"].startswith("image/"):
            if details["image_pixels"] > MAX_IMAGE_PIXELS:
                raise DocumentInvalid()
            total_pixels += details["image_pixels"]
    if total_pixels > MAX_TOTAL_IMAGE_PIXELS:
        raise DocumentInvalid()
    is_pdf = inspected[0]["mime_type"] == "application/pdf"
    if any((item["mime_type"] == "application/pdf") != is_pdf for item in inspected):
        raise DocumentInvalid()
    if is_pdf and (len(files) != 1 or metadata["source_kind"] != "pdf_import"):
        raise DocumentInvalid()
    if not is_pdf and metadata["source_kind"] == "pdf_import":
        raise DocumentInvalid()
    expected = (
        [(0, page) for page in range(1, inspected[0]["page_count"] + 1)]
        if is_pdf
        else [(index, 1) for index in range(len(files))]
    )
    actual = [(page["asset_index"], page["source_page"]) for page in metadata["pages"]]
    if actual != expected:
        raise _error("invalid_pages", 400)
    return inspected
