"""Authenticated, patient-scoped prescription and immutable document reads."""

import hashlib
import uuid

from django.core.files.storage import default_storage
from django.db.models import F
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response

from nurse.document_storage import open_private
from nurse.document_validation import MAX_ORIGINAL_BYTES
from nurse.models import (
    Patient,
    Prescription,
    PrescriptionDocumentAsset,
    PrescriptionDocumentVersion,
)
from nurse.read_api import PAGE_PARAMETERS, PrivateReadView, ReadAPIError
from nurse.read_document_serializers import (
    DocumentManifestSerializer,
    DocumentReadErrorSerializer,
    PrescriptionV3PageSerializer,
)
from nurse.read_prescription_serializers import (
    PrescriptionDetailQuerySerializer,
    PrescriptionSummarySerializer,
)

LEGACY_ASSET_NAMESPACE = uuid.UUID("ca2dbb21-13d0-5abc-a692-8d4a4af91d98")
PATIENT_ID = OpenApiParameter("patient_id", int, OpenApiParameter.PATH)
PRESCRIPTION_ID = OpenApiParameter("prescription_id", int, OpenApiParameter.PATH)
ASSET_ID = OpenApiParameter("asset_id", uuid.UUID, OpenApiParameter.PATH)
ERROR_RESPONSES = {
    code: DocumentReadErrorSerializer for code in (400, 401, 404, 405, 429, 503)
}


class PrescriptionReadView(PrivateReadView):
    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if isinstance(response.data, dict) and response.status_code >= 400:
            response.data.setdefault("fields", {})
        return response


class ScopedPrescriptionView(PrescriptionReadView):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.patient = get_object_or_404(
            Patient.objects.filter(nurse__user=request.user).only("id"),
            pk=kwargs["patient_id"],
        )
        self.prescription = get_object_or_404(
            Prescription.objects.filter(patient=self.patient).only(
                "id",
                "patient_id",
                "prescribing_doctor",
                "start_date",
                "end_date",
                "photo_prescription",
            ),
            pk=kwargs["prescription_id"],
        )

    def options(self, request, *args, **kwargs):
        return Response({"allowed_methods": ", ".join(self.allowed_methods)})


class PrescriptionList(PrescriptionReadView):
    @extend_schema(
        operation_id="v3_prescriptions_list",
        parameters=[PATIENT_ID, *PAGE_PARAMETERS],
        responses={200: PrescriptionV3PageSerializer, **ERROR_RESPONSES},
    )
    def get(self, request, patient_id):
        patient = get_object_or_404(
            Patient.objects.filter(nurse__user=request.user).only("id"), pk=patient_id
        )
        query = self.query(request)
        rows = (
            Prescription.objects.filter(patient=patient)
            .order_by(F("end_date").desc(nulls_last=True), "-id")
            .only("id", "patient_id", "prescribing_doctor", "start_date", "end_date")
        )
        return self.page_response(rows, query, PrescriptionSummarySerializer)

    def options(self, request, *args, **kwargs):
        get_object_or_404(
            Patient.objects.filter(nurse__user=request.user).only("id"),
            pk=kwargs["patient_id"],
        )
        return Response({"allowed_methods": ", ".join(self.allowed_methods)})


class PrescriptionDetail(ScopedPrescriptionView):
    query_serializer_class = PrescriptionDetailQuerySerializer

    @extend_schema(
        operation_id="v3_prescriptions_retrieve",
        parameters=[PATIENT_ID, PRESCRIPTION_ID],
        responses={200: PrescriptionSummarySerializer, **ERROR_RESPONSES},
    )
    def get(self, request, **kwargs):
        self.query(request)
        if request.method == "HEAD":
            return Response(status=200)
        return Response(PrescriptionSummarySerializer(self.prescription).data)


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


def _legacy_asset(prescription):
    field = prescription.photo_prescription
    name = getattr(field, "name", "")
    if not name:
        raise Http404
    try:
        with default_storage.open(name, "rb") as stream:
            content = stream.read(MAX_ORIGINAL_BYTES + 1)
    except Exception as error:
        raise ReadAPIError("service_unavailable") from error
    if not content or len(content) > MAX_ORIGINAL_BYTES:
        raise ReadAPIError("service_unavailable")
    if content.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    else:
        raise Http404
    return {
        "asset_id": str(uuid.uuid5(LEGACY_ASSET_NAMESPACE, str(prescription.pk))),
        "mime_type": mime,
        "byte_size": len(content),
        "page_count": 1,
        "sha256": hashlib.sha256(content).hexdigest(),
        "role": "original",
        "original_index": 0,
        "source_asset_id": None,
    }, content


class PrescriptionDocumentManifest(ScopedPrescriptionView):
    @extend_schema(
        operation_id="v3_prescription_document_manifest",
        parameters=[PATIENT_ID, PRESCRIPTION_ID],
        responses={200: DocumentManifestSerializer, **ERROR_RESPONSES},
    )
    def get(self, request, **kwargs):
        self.query(request)
        if request.method == "HEAD":
            return Response(status=200)
        version = (
            PrescriptionDocumentVersion.objects.filter(prescription=self.prescription)
            .order_by("-version")
            .first()
        )
        if version is None:
            asset, _ = _legacy_asset(self.prescription)
            return Response(
                {
                    "version": 1,
                    "assets": [asset],
                    "pages": [
                        {
                            "source_asset_id": asset["asset_id"],
                            "source_page": 1,
                            "display_asset_id": asset["asset_id"],
                            "display_page": 1,
                            "rotation": 0,
                            "crop": None,
                        }
                    ],
                }
            )
        assets = list(
            version.assets.select_related("source_asset").order_by(
                "role", "original_index", "created_at"
            )
        )
        originals = {asset.pk: asset for asset in assets if asset.role == "original"}
        derived = {
            asset.source_asset_id: asset for asset in assets if asset.role == "derived"
        }
        metadata = version.document_import.metadata or {}
        pages = []
        for page in metadata.get("pages", []):
            source = next(
                (
                    asset
                    for asset in originals.values()
                    if asset.original_index == page["asset_index"]
                ),
                None,
            )
            if source is None:
                raise ReadAPIError("service_unavailable")
            display = derived.get(source.pk, source)
            pages.append(
                {
                    "source_asset_id": str(source.pk),
                    "source_page": page["source_page"],
                    "display_asset_id": str(display.pk),
                    "display_page": 1
                    if display.role == "derived"
                    else page["source_page"],
                    "rotation": page["rotation"],
                    "crop": page["crop"],
                }
            )
        return Response(
            {
                "version": version.version,
                "assets": [_asset_data(a) for a in assets],
                "pages": pages,
            }
        )


class PrescriptionDocumentContent(ScopedPrescriptionView):
    def _resolve(self, asset_id):
        version = (
            PrescriptionDocumentVersion.objects.filter(prescription=self.prescription)
            .order_by("-version")
            .first()
        )
        if version is None:
            if asset_id != uuid.uuid5(
                LEGACY_ASSET_NAMESPACE, str(self.prescription.pk)
            ):
                raise Http404
            asset, content = _legacy_asset(self.prescription)
            return None, asset, content
        asset = get_object_or_404(
            PrescriptionDocumentAsset.objects.filter(document_version=version),
            pk=asset_id,
        )
        return asset, _asset_data(asset), None

    @extend_schema(
        operation_id="v3_prescription_document_content",
        parameters=[PATIENT_ID, PRESCRIPTION_ID, ASSET_ID],
        responses={200: bytes, **ERROR_RESPONSES},
    )
    def get(self, request, **kwargs):
        self.query(request)
        if request.method == "HEAD":
            version = (
                PrescriptionDocumentVersion.objects.filter(
                    prescription=self.prescription
                )
                .order_by("-version")
                .first()
            )
            if version is None:
                expected = uuid.uuid5(LEGACY_ASSET_NAMESPACE, str(self.prescription.pk))
                if (
                    kwargs["asset_id"] != expected
                    or not self.prescription.photo_prescription.name
                ):
                    raise Http404
                name = self.prescription.photo_prescription.name.lower()
                extension = "png" if name.endswith(".png") else "jpg"
                mime = "image/png" if extension == "png" else "image/jpeg"
                size = None
            else:
                asset = get_object_or_404(
                    PrescriptionDocumentAsset.objects.filter(document_version=version),
                    pk=kwargs["asset_id"],
                )
                mime, size = asset.mime_type, asset.byte_size
            response = HttpResponse(status=200, content_type=mime)
            if size is not None:
                response["Content-Length"] = str(size)
            else:
                response["Content-Length"] = "0"
            extension = (
                "pdf"
                if mime == "application/pdf"
                else "png"
                if mime == "image/png"
                else "jpg"
            )
            response["Content-Disposition"] = (
                f'attachment; filename="document.{extension}"'
            )
            response["X-Content-Type-Options"] = "nosniff"
            return response
        asset, data, legacy_content = self._resolve(kwargs["asset_id"])
        if legacy_content is not None:
            response = HttpResponse(legacy_content, content_type=data["mime_type"])
            response["Content-Disposition"] = (
                'attachment; filename="document.'
                + ("png" if data["mime_type"] == "image/png" else "jpg")
                + '"'
            )
        else:
            try:
                handle = open_private(asset.storage_key)
            except Exception as error:
                raise ReadAPIError("service_unavailable") from error
            filename = (
                "document.pdf"
                if data["mime_type"] == "application/pdf"
                else "document.png"
                if data["mime_type"] == "image/png"
                else "document.jpg"
            )
            response = FileResponse(
                handle,
                content_type=data["mime_type"],
                as_attachment=True,
                filename=filename,
            )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        return response

    def options(self, request, *args, **kwargs):
        self.query(request)
        # Resolve scope and UUID without opening or hashing any file.
        version = (
            PrescriptionDocumentVersion.objects.filter(prescription=self.prescription)
            .order_by("-version")
            .first()
        )
        if version is None:
            expected = uuid.uuid5(LEGACY_ASSET_NAMESPACE, str(self.prescription.pk))
            if kwargs["asset_id"] != expected:
                raise Http404
        else:
            get_object_or_404(
                PrescriptionDocumentAsset.objects.filter(document_version=version),
                pk=kwargs["asset_id"],
            )
        return Response({"allowed_methods": ", ".join(self.allowed_methods)})
