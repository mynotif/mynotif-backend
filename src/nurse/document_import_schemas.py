"""Closed v3 request and response schemas for document imports."""

import hashlib
import json
import math
import re
import uuid
from datetime import date

from rest_framework.exceptions import APIException

from nurse.document_validation import MAX_ORIGINAL_BYTES, MAX_PAGES, DocumentTooLarge
from nurse.models import DocumentImportState

IMPORT_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class ImportError(APIException):
    status_code = 400
    default_detail = {"code": "invalid_metadata"}
    default_code = "invalid_metadata"


class ImportConflict(APIException):
    status_code = 409
    default_detail = {"code": "invalid_state"}
    default_code = "invalid_state"


class RevisionConflict(APIException):
    status_code = 412
    default_detail = {"code": "revision_conflict"}
    default_code = "revision_conflict"


class ImportExpired(APIException):
    status_code = 410
    default_detail = {"code": "import_expired"}
    default_code = "import_expired"


def _error(code, status):
    error = APIException({"code": code})
    error.status_code = status
    return error


def _closed_object(value, required):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ImportError()


def _validate_metadata(value):
    _closed_object(
        value,
        {"prescribing_doctor", "start_date", "end_date", "source_kind", "pages"},
    )
    doctor = value["prescribing_doctor"]
    if not isinstance(doctor, str) or len(doctor) > 300:
        raise ImportError()
    if value["source_kind"] not in {"camera", "image_import", "pdf_import"}:
        raise ImportError()
    dates = {}
    for field in ("start_date", "end_date"):
        raw = value[field]
        if raw is None:
            dates[field] = None
        elif isinstance(raw, str):
            try:
                parsed = date.fromisoformat(raw)
            except ValueError as error:
                raise _error("invalid_dates", 400) from error
            if parsed.isoformat() != raw:
                raise _error("invalid_dates", 400)
            dates[field] = raw
        else:
            raise _error("invalid_dates", 400)
    if dates["start_date"] and dates["end_date"]:
        if date.fromisoformat(dates["start_date"]) > date.fromisoformat(
            dates["end_date"]
        ):
            raise _error("invalid_dates", 400)
    pages = value["pages"]
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise _error("invalid_pages", 400)
    clean_pages = []
    for page in pages:
        _closed_object(page, {"asset_index", "source_page", "rotation", "crop"})
        index, source_page, rotation, crop = (
            page["asset_index"],
            page["source_page"],
            page["rotation"],
            page["crop"],
        )
        if (
            type(index) is not int
            or type(source_page) is not int
            or type(rotation) is not int
            or index < 0
            or source_page < 1
            or rotation not in (0, 90, 180, 270)
        ):
            raise _error("invalid_pages", 400)
        if crop is not None:
            _closed_object(crop, {"x", "y", "width", "height"})
            values = [crop[key] for key in ("x", "y", "width", "height")]
            if any(
                type(number) not in (int, float) or not math.isfinite(number)
                for number in values
            ):
                raise _error("invalid_pages", 400)
            x, y, width, height = values
            if (
                min(values) < 0
                or width <= 0
                or height <= 0
                or x + width > 1
                or y + height > 1
            ):
                raise _error("invalid_pages", 400)
            crop = {key: float(crop[key]) for key in ("x", "y", "width", "height")}
        clean_pages.append(
            {
                "asset_index": index,
                "source_page": source_page,
                "rotation": rotation,
                "crop": crop,
            }
        )
    if clean_pages != sorted(
        clean_pages, key=lambda item: (item["asset_index"], item["source_page"])
    ):
        raise _error("invalid_pages", 400)
    return {
        "prescribing_doctor": doctor,
        **dates,
        "source_kind": value["source_kind"],
        "pages": clean_pages,
    }


def _read_metadata(request, *, multipart=False):
    if multipart:
        allowed = {"metadata"} | set(request.FILES.keys())
        if (
            set(request.data.keys()) != allowed
            or len(request.data.getlist("metadata")) != 1
        ):
            raise ImportError()
        raw = request.data.get("metadata")
    else:
        raw = request.data
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > 65536:
            raise ImportError()
        try:
            raw = json.loads(
                raw,
                object_pairs_hook=_unique_json_object,
            )
        except (TypeError, ValueError) as error:
            raise ImportError() from error
    else:
        try:
            if len(json.dumps(raw, separators=(",", ":")).encode("utf-8")) > 65536:
                raise ImportError()
        except (TypeError, ValueError) as error:
            raise ImportError() from error
    return _validate_metadata(raw)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _files(request):
    keys = set(request.FILES.keys())
    count = len(keys)
    if not 1 <= count <= MAX_PAGES or keys != {f"original_{i}" for i in range(count)}:
        raise _error("invalid_document", 422)
    result, total = [], 0
    for index in range(count):
        if len(request.FILES.getlist(f"original_{index}")) != 1:
            raise _error("invalid_document", 422)
        upload = request.FILES.get(f"original_{index}")
        content = upload.read(MAX_ORIGINAL_BYTES + 1) if upload else b""
        total += len(content)
        if not content or total > MAX_ORIGINAL_BYTES:
            raise DocumentTooLarge()
        result.append(content)
    return result


def _fingerprint(patient_id, metadata, files):
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    hashes = [hashlib.sha256(content).hexdigest() for content in files]
    body = json.dumps([patient_id, canonical, hashes], separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _asset_data(asset):
    return {
        "asset_id": str(asset.pk),
        "mime_type": asset.mime_type,
        "byte_size": asset.byte_size,
        "page_count": asset.page_count,
        "sha256": asset.sha256,
        "role": asset.role,
        "original_index": asset.original_index,
        "source_asset_id": str(asset.source_asset_id)
        if asset.source_asset_id
        else None,
    }


def _serialize(operation):
    assets = list(operation.assets.order_by("original_index", "created_at"))
    by_index = {
        asset.original_index: asset for asset in assets if asset.role == "original"
    }
    derived_by_source = {
        str(asset.source_asset_id): asset for asset in assets if asset.role == "derived"
    }
    ready = operation.state in (
        DocumentImportState.READY,
        DocumentImportState.CONFIRMED,
    )
    metadata = operation.metadata if ready else None
    pages = []
    if ready and metadata:
        for page in metadata["pages"]:
            asset = by_index[page["asset_index"]]
            derived = derived_by_source.get(str(asset.pk))
            pages.append(
                {
                    "source_asset_id": str(asset.pk),
                    "source_page": page["source_page"],
                    "display_asset_id": str(derived.pk if derived else asset.pk),
                    "display_page": 1 if derived else page["source_page"],
                    "rotation": page["rotation"],
                    "crop": page["crop"],
                }
            )
    return {
        "import_id": str(operation.import_uuid),
        "patient_id": operation.patient_id,
        "state": operation.state,
        "revision": operation.revision,
        "created_at": operation.created_at.isoformat().replace("+00:00", "Z"),
        "expires_at": operation.expires_at.isoformat().replace("+00:00", "Z"),
        "metadata": metadata,
        "assets": [_asset_data(asset) for asset in assets] if ready else [],
        "pages": pages,
        "prescription_id": operation.prescription_id,
        "error_code": (
            "validation_unavailable"
            if operation.state == DocumentImportState.RETRYABLE
            else "invalid_document"
            if operation.state == DocumentImportState.REJECTED
            else None
        ),
    }


def _parse_import_uuid(request):
    raw = request.headers.get("Idempotency-Key", "")
    if not IMPORT_UUID_RE.fullmatch(raw):
        raise ImportError()
    return uuid.UUID(raw)


def _if_match(request, operation):
    match = re.fullmatch(r'"([1-9][0-9]*)"', request.headers.get("If-Match", ""))
    if not match:
        raise ImportError()
    if int(match.group(1)) != operation.revision:
        raise RevisionConflict()


def _recorded(operation, version):
    prescription = operation.prescription
    assets = list(version.assets.order_by("created_at"))
    by_index = {
        asset.original_index: asset for asset in assets if asset.role == "original"
    }
    derived_by_source = {
        str(asset.source_asset_id): asset for asset in assets if asset.role == "derived"
    }
    pages = []
    for page in operation.metadata["pages"]:
        source = by_index[page["asset_index"]]
        derived = derived_by_source.get(str(source.pk))
        pages.append(
            {
                "source_asset_id": str(source.pk),
                "source_page": page["source_page"],
                "display_asset_id": str(derived.pk if derived else source.pk),
                "display_page": 1 if derived else page["source_page"],
                "rotation": page["rotation"],
                "crop": page["crop"],
            }
        )
    return {
        "import_id": str(operation.import_uuid),
        "patient_id": operation.patient_id,
        "prescription_id": prescription.pk,
        "revision": version.document_import.revision,
        "document_version": version.version,
        "prescribing_doctor": version.prescribing_doctor,
        "start_date": version.start_date.isoformat() if version.start_date else None,
        "end_date": version.end_date.isoformat() if version.end_date else None,
        "recorded_at": version.created_at.isoformat().replace("+00:00", "Z"),
        "assets": [_asset_data(asset) for asset in assets],
        "pages": pages,
    }


def _validate_page_manifest(metadata, assets):
    expected = []
    for asset in assets:
        expected.extend(
            (asset.original_index, page) for page in range(1, asset.page_count + 1)
        )
    actual = [(page["asset_index"], page["source_page"]) for page in metadata["pages"]]
    if expected != actual:
        raise _error("invalid_pages", 400)
    if any(
        asset.mime_type == "application/pdf"
        and any(page["crop"] is not None for page in metadata["pages"])
        for asset in assets
    ):
        raise _error("invalid_pages", 400)
