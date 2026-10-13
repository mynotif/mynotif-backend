"""Authenticated, patient-scoped dispatch for the v3 import API."""

import uuid
from datetime import timedelta

from django.core.files.storage import default_storage
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response

from nurse.document_import_schemas import (
    ImportConflict,
    ImportExpired,
    RevisionConflict,
    _closed_object,
    _error,
    _files,
    _fingerprint,
    _if_match,
    _parse_import_uuid,
    _read_metadata,
    _recorded,
    _serialize,
    _validate_page_manifest,
)
from nurse.document_import_services import _reserve, _validate_files
from nurse.document_storage import delete_private, open_private, save_staging
from nurse.document_validation import (
    MAX_IMAGE_PIXELS,
    MAX_ORIGINAL_BYTES,
    MAX_PAGES,
    MAX_TOTAL_IMAGE_PIXELS,
    ValidationUnavailable,
    derive_image,
)
from nurse.models import (
    DocumentImport,
    DocumentImportAttempt,
    DocumentImportState,
    Nurse,
    Patient,
    Prescription,
    PrescriptionDocumentAsset,
    PrescriptionDocumentVersion,
)
from nurse.read_serializers import ReadQuerySerializer

def _patient(request, patient_id):
    if not Nurse.objects.filter(user=request.user).exists():
        from django.http import Http404

        raise Http404
    return get_object_or_404(
        Patient.objects.filter(nurse__user=request.user).only("id"), pk=patient_id
    )


def _operation(request, patient, import_uuid):
    return get_object_or_404(
        DocumentImport.objects.filter(
            owner=request.user, patient=patient, import_uuid=import_uuid
        ).prefetch_related("assets"),
        import_uuid=import_uuid,
    )


def _derived_payloads(metadata, assets, original_bytes):
    by_index = {asset.original_index: asset for asset in assets if asset.role == "original"}
    result, total = [], 0
    for page in metadata["pages"]:
        if not page["rotation"] and page["crop"] is None:
            continue
        source = by_index[page["asset_index"]]
        if source.mime_type == "application/pdf":
            if page["crop"] is not None:
                raise _error("invalid_pages", 400)
            continue
        content = original_bytes.get(source.original_index)
        if content is None:
            try:
                with open_private(source.storage_key) as stream:
                    content = stream.read(MAX_ORIGINAL_BYTES + 1)
            except Exception as error:
                raise ValidationUnavailable() from error
        derivative, details = derive_image(
            content, {"crop": page["crop"], "rotation": page["rotation"]}
        )
        total += len(derivative)
        if total > MAX_ORIGINAL_BYTES:
            raise _error("document_too_large", 413)
        result.append((source, derivative, details))
    return result


def _store_derivatives(operation, payloads, stored_keys):
    for source, content, details in payloads:
        key = save_staging(operation.pk, "derived", content, "jpg")
        stored_keys.append(key)
        PrescriptionDocumentAsset.objects.create(
            document_import=operation,
            role=PrescriptionDocumentAsset.Role.DERIVED,
            source_asset=source,
            storage_key=key,
            mime_type=details["mime_type"],
            byte_size=details["byte_size"],
            page_count=1,
            sha256=details["sha256"],
        )


def _record_import_attempt(request):
    """Persist a per-account attempt before parsing or buffering the multipart body."""
    with transaction.atomic():
        Nurse.objects.select_for_update().get(user=request.user)
        now = timezone.now()
        DocumentImportAttempt.objects.filter(
            owner=request.user, attempted_at__lt=now - timedelta(days=1)
        ).delete()
        recent = DocumentImportAttempt.objects.filter(
            owner=request.user, attempted_at__gte=now - timedelta(minutes=1)
        ).count()
        if recent >= 3:
            raise _error("rate_limited", 429)
        DocumentImportAttempt.objects.create(owner=request.user)


def dispatch(operation, request, **kwargs):
    if operation == "capabilities":
        if request.method == "OPTIONS":
            return Response({"allowed_methods": "GET, HEAD, OPTIONS"})
        if request.method == "HEAD":
            return Response(status=200)
        if request.method != "GET":
            return Response(status=405)
        return Response(
            {
                "contract_version": 1,
                "mime_types": ["image/jpeg", "image/png", "application/pdf"],
                "max_original_bytes": MAX_ORIGINAL_BYTES,
                "max_pages": MAX_PAGES,
                "max_image_pixels": MAX_IMAGE_PIXELS,
                "max_total_image_pixels": MAX_TOTAL_IMAGE_PIXELS,
                "max_derived_bytes": MAX_ORIGINAL_BYTES,
                "max_metadata_bytes": 65536,
                "draft_ttl_seconds": 86400,
            }
        )

    patient = _patient(request, kwargs["patient_id"])
    if request.method == "OPTIONS":
        methods = {
            "imports": "GET, HEAD, POST, OPTIONS",
            "import": "GET, HEAD, PATCH, DELETE, OPTIONS",
            "confirmation": "PUT, OPTIONS",
            "import-asset-content": "GET, HEAD, OPTIONS",
        }.get(operation)
        if methods is None:
            raise _error("not_found", 404)
        if operation != "imports":
            draft = _operation(request, patient, kwargs["import_uuid"])
            if operation == "import-asset-content":
                if draft.state not in (
                    DocumentImportState.READY,
                    DocumentImportState.CONFIRMED,
                ):
                    raise _error("invalid_state", 409)
                get_object_or_404(draft.assets.all(), pk=kwargs["asset_id"])
        return Response({"allowed_methods": methods})

    if operation == "imports" and request.method == "HEAD":
        return Response(status=200)

    if operation == "imports" and request.method == "GET":
        query = ReadQuerySerializer(data=request.query_params)
        if not query.is_valid():
            raise _error("invalid_metadata", 400)
        page = query.validated_data["page"]
        page_size = query.validated_data["page_size"]
        queryset = DocumentImport.objects.filter(
            owner=request.user, patient=patient, expires_at__gt=timezone.now()
        ).order_by("-created_at")
        total = queryset.count()
        last_page = max(1, (total + page_size - 1) // page_size)
        if page > last_page:
            raise _error("not_found", 404)
        start = (page - 1) * page_size
        results = [
            _serialize(item) for item in queryset[start : start + page_size]
        ]
        return Response(
            {
                "count": total,
                "page": page,
                "page_size": page_size,
                "next_page": page + 1 if page < last_page else None,
                "previous_page": page - 1 if page > 1 else None,
                "results": results,
            }
        )

    if operation == "imports" and request.method == "POST":
        import_uuid = _parse_import_uuid(request)
        _record_import_attempt(request)
        metadata, files = _read_metadata(request, multipart=True), _files(request)
        fingerprint = _fingerprint(patient.pk, metadata, files)
        draft, lease, owns_lease = _reserve(request, patient, import_uuid, fingerprint)
        if not owns_lease:
            return Response(_serialize(draft), status=200)
        stored_keys = []
        try:
            with transaction.atomic():
                DocumentImport.objects.filter(pk=draft.pk, lease_token=lease).update(
                    state=DocumentImportState.VALIDATING
                )
            inspected = _validate_files(files, metadata)
            original_assets = []
            for index, (content, details) in enumerate(zip(files, inspected)):
                ext = "pdf" if details["mime_type"] == "application/pdf" else "png" if details["mime_type"] == "image/png" else "jpg"
                key = save_staging(draft.pk, "original", content, ext)
                stored_keys.append(key)
                with transaction.atomic():
                    locked = DocumentImport.objects.select_for_update().get(pk=draft.pk)
                    if locked.lease_token != lease or locked.state != DocumentImportState.VALIDATING:
                        raise ImportConflict({"code": "operation_in_progress"})
                    original_assets.append(PrescriptionDocumentAsset.objects.create(
                        document_import=locked,
                        role=PrescriptionDocumentAsset.Role.ORIGINAL,
                        original_index=index,
                        storage_key=key,
                        mime_type=details["mime_type"],
                        byte_size=details["byte_size"],
                        page_count=details["page_count"],
                        sha256=details["sha256"],
                    ))
            _store_derivatives(
                draft,
                _derived_payloads(metadata, original_assets, dict(enumerate(files))),
                stored_keys,
            )
            with transaction.atomic():
                locked = DocumentImport.objects.select_for_update().get(pk=draft.pk)
                if (
                    locked.lease_token != lease
                    or locked.expires_at <= timezone.now()
                    or locked.lease_expires_at <= timezone.now()
                ):
                    raise ImportConflict({"code": "operation_in_progress"})
                locked.metadata = metadata
                locked.state = DocumentImportState.READY
                locked.lease_token = None
                locked.lease_expires_at = None
                locked.save(update_fields=("metadata", "state", "lease_token", "lease_expires_at", "updated_at"))
            draft.refresh_from_db()
            return Response(_serialize(draft), status=202)
        except Exception as error:
            with transaction.atomic():
                locked = DocumentImport.objects.select_for_update().get(pk=draft.pk)
                if locked.lease_token == lease and locked.state != DocumentImportState.CONFIRMED:
                    retryable = getattr(error, "status_code", 400) >= 500
                    locked.state = DocumentImportState.RETRYABLE if retryable else DocumentImportState.REJECTED
                    locked.lease_token = None
                    locked.lease_expires_at = None
                    if not retryable:
                        locked.metadata = None
                    locked.save(update_fields=("state", "lease_token", "lease_expires_at", "metadata", "updated_at"))
                    for key in stored_keys:
                        delete_private(key)
                    staging = PrescriptionDocumentAsset.objects.filter(
                        document_import=locked, document_version__isnull=True
                    )
                    staging.filter(role=PrescriptionDocumentAsset.Role.DERIVED).delete()
                    staging.filter(role=PrescriptionDocumentAsset.Role.ORIGINAL).delete()
            raise

    if operation not in {"import", "confirmation", "import-asset-content"}:
        raise _error("not_found", 404)
    draft = _operation(request, patient, kwargs["import_uuid"])

    if request.method == "HEAD":
        if operation == "import":
            if draft.expires_at <= timezone.now() and draft.state != DocumentImportState.CONFIRMED:
                raise ImportExpired()
            return Response(status=200)
        if operation == "import-asset-content":
            if draft.state not in (DocumentImportState.READY, DocumentImportState.CONFIRMED):
                raise ImportConflict()
            asset = get_object_or_404(draft.assets.all(), pk=kwargs["asset_id"])
            response = Response(status=200)
            response["Content-Type"] = asset.mime_type
            response["Content-Length"] = str(asset.byte_size)
            response["Content-Disposition"] = 'attachment; filename="document"'
            response["X-Content-Type-Options"] = "nosniff"
            return response

    if operation == "import" and request.method == "GET":
        if draft.expires_at <= timezone.now() and draft.state != DocumentImportState.CONFIRMED:
            raise ImportExpired()
        return Response(_serialize(draft))

    if operation == "import" and request.method == "PATCH":
        metadata = _read_metadata(request)
        with transaction.atomic():
            draft = DocumentImport.objects.select_for_update().get(pk=draft.pk)
            _if_match(request, draft)
            if draft.state != DocumentImportState.READY:
                raise ImportConflict()
            assets = list(draft.assets.filter(role=PrescriptionDocumentAsset.Role.ORIGINAL).order_by("original_index"))
            _validate_page_manifest(metadata, assets)
            lease = uuid.uuid4()
            draft.state = DocumentImportState.VALIDATING
            draft.lease_token = lease
            draft.lease_expires_at = timezone.now() + timedelta(seconds=120)
            draft.save(update_fields=("state", "lease_token", "lease_expires_at", "updated_at"))
        old_derived = list(draft.assets.filter(role=PrescriptionDocumentAsset.Role.DERIVED))
        old_keys, stored_keys = [], []
        try:
            payloads = _derived_payloads(metadata, assets, {})
            prepared = []
            for source, content, details in payloads:
                key = save_staging(draft.pk, "derived", content, "jpg")
                stored_keys.append(key)
                prepared.append((source, key, details))
            with transaction.atomic():
                locked = DocumentImport.objects.select_for_update().get(pk=draft.pk)
                if locked.lease_token != lease or locked.state != DocumentImportState.VALIDATING:
                    raise ImportConflict({"code": "operation_in_progress"})
                old_keys = [asset.storage_key for asset in old_derived]
                PrescriptionDocumentAsset.objects.filter(
                    pk__in=[asset.pk for asset in old_derived]
                ).delete()
                for source, key, details in prepared:
                    PrescriptionDocumentAsset.objects.create(
                        document_import=locked,
                        role=PrescriptionDocumentAsset.Role.DERIVED,
                        source_asset=source,
                        storage_key=key,
                        mime_type=details["mime_type"],
                        byte_size=details["byte_size"],
                        page_count=1,
                        sha256=details["sha256"],
                    )
                locked.metadata = metadata
                locked.revision += 1
                locked.state = DocumentImportState.READY
                locked.lease_token = None
                locked.lease_expires_at = None
                locked.save(update_fields=(
                    "metadata", "revision", "state", "lease_token",
                    "lease_expires_at", "updated_at",
                ))
        except Exception:
            for key in stored_keys:
                delete_private(key)
            with transaction.atomic():
                locked = DocumentImport.objects.select_for_update().get(pk=draft.pk)
                if locked.lease_token == lease and locked.state == DocumentImportState.VALIDATING:
                    locked.state = DocumentImportState.READY
                    locked.lease_token = None
                    locked.lease_expires_at = None
                    locked.save(update_fields=("state", "lease_token", "lease_expires_at", "updated_at"))
            raise
        for key in old_keys:
            delete_private(key)
        draft.refresh_from_db()
        return Response(_serialize(draft))

    if operation == "import" and request.method == "DELETE":
        with transaction.atomic():
            draft = DocumentImport.objects.select_for_update().get(pk=draft.pk)
            if draft.state == DocumentImportState.CONFIRMED:
                raise ImportConflict({"code": "already_confirmed"})
            if draft.state != DocumentImportState.ABANDONED:
                _if_match(request, draft)
                draft.state = DocumentImportState.ABANDONED
                draft.metadata = None
                draft.lease_token = None
                draft.lease_expires_at = None
                draft.save(update_fields=("state", "metadata", "lease_token", "lease_expires_at", "updated_at"))
                staging = draft.assets.filter(document_version__isnull=True)
                keys = list(staging.values_list("storage_key", flat=True))
                for key in keys:
                    delete_private(key)
                staging.filter(role=PrescriptionDocumentAsset.Role.DERIVED).delete()
                staging.filter(role=PrescriptionDocumentAsset.Role.ORIGINAL).delete()
        return Response(status=204)

    if operation == "confirmation" and request.method == "PUT":
        _closed_object(request.data, {"revision", "patient_confirmed", "pages_confirmed", "dates_reviewed"})
        if (
            type(request.data["revision"]) is not int
            or any(
                request.data[key] is not True
                for key in ("patient_confirmed", "pages_confirmed", "dates_reviewed")
            )
        ):
            raise ImportConflict()
        with transaction.atomic():
            draft = DocumentImport.objects.select_for_update().get(pk=draft.pk)
            if draft.state == DocumentImportState.CONFIRMED:
                if request.data["revision"] != draft.revision:
                    raise RevisionConflict()
                return Response(_recorded(draft, draft.document_version), status=200)
            if draft.expires_at <= timezone.now():
                raise ImportExpired()
            if draft.state != DocumentImportState.READY or request.data["revision"] != draft.revision:
                raise RevisionConflict()
            assets = list(draft.assets.all())
            originals = [asset for asset in assets if asset.role == PrescriptionDocumentAsset.Role.ORIGINAL]
            if not originals:
                raise ImportConflict()
            for asset in assets:
                try:
                    if not default_storage.exists(asset.storage_key):
                        raise ValidationUnavailable()
                except OSError as error:
                    raise ValidationUnavailable() from error
            metadata = draft.metadata
            prescription = Prescription.objects.create(
                patient=patient,
                prescribing_doctor=metadata["prescribing_doctor"],
                start_date=metadata["start_date"],
                end_date=metadata["end_date"],
                # v3 bytes are addressed only through PrescriptionDocumentAsset.
                # Never place a private v3 key in the legacy ImageField.
                photo_prescription="",
            )
            version = PrescriptionDocumentVersion.objects.create(
                prescription=prescription,
                document_import=draft,
                version=1,
                prescribing_doctor=metadata["prescribing_doctor"],
                start_date=metadata["start_date"],
                end_date=metadata["end_date"],
            )
            draft.prescription = prescription
            draft.state = DocumentImportState.CONFIRMED
            draft.save(update_fields=("prescription", "state", "updated_at"))
            draft.assets.update(document_version=version)
        draft.refresh_from_db()
        return Response(_recorded(draft, version), status=201)

    if operation == "import-asset-content" and request.method == "GET":
        if draft.state not in (DocumentImportState.READY, DocumentImportState.CONFIRMED):
            raise ImportConflict()
        asset = get_object_or_404(draft.assets.all(), pk=kwargs["asset_id"])
        try:
            handle = open_private(asset.storage_key)
        except Exception as error:
            raise ValidationUnavailable() from error
        filename = "document.pdf" if asset.mime_type == "application/pdf" else "document.png" if asset.mime_type == "image/png" else "document.jpg"
        response = FileResponse(handle, content_type=asset.mime_type, as_attachment=True, filename=filename)
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        return response
    return Response(status=405)
