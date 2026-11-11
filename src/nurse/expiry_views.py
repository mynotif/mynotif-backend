"""Private expiry reads: authenticate, scope, snapshot, project, validate."""

import math
import re
from datetime import date, timedelta

from django.core.exceptions import BadRequest, SuspiciousOperation
from django.core.files.storage import default_storage
from django.db import DatabaseError
from django.db.models import F, OuterRef, Prefetch, Q, Subquery
from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.authentication import get_authorization_header
from rest_framework.exceptions import (
    AuthenticationFailed,
    MethodNotAllowed,
    NotAuthenticated,
    NotFound,
    Throttled,
    UnsupportedMediaType,
    ValidationError,
)
from rest_framework.response import Response

from main.models import AuthToken
from main.session_services import fingerprint
from main.session_tokens import parse_token, verify_secret
from nurse.expiry_calculation import classify_dates
from nurse.expiry_models import ExpiryPrescriptionState
from nurse.expiry_serializers import (
    FILTERS,
    ORDERINGS,
    ExpiryDetailSerializer,
    ExpiryErrorSerializer,
    ExpiryPageSerializer,
    ExpiryQuerySerializer,
    ExpirySchema,
)
from nurse.expiry_state import (
    ERRORS,
    ExpiryError,
    authority,
    cursor_context,
    cursor_offset,
    finish_read,
    next_cursor,
    read_context,
)
from nurse.models import (
    Nurse,
    Prescription,
    PrescriptionDocumentAsset,
    PrescriptionDocumentVersion,
)
from nurse.read_api import PrivateReadView

ERROR_RESPONSES = {status: ExpiryErrorSerializer for status, _ in ERRORS.values()}
QUERY_PARAMETERS = [
    OpenApiParameter("filter", str, enum=FILTERS, default="all"),
    OpenApiParameter("ordering", str, enum=ORDERINGS, default="end_date_asc"),
    OpenApiParameter(
        "page_size", {"type": "integer", "minimum": 1, "maximum": 100}, default=20
    ),
    OpenApiParameter("cursor", {"type": "string", "maxLength": 2048}),
]
ID_SCHEMA = {"type": "integer", "minimum": 1, "maximum": 9007199254740991}
DETAIL_PARAMETERS = [
    OpenApiParameter("patient_id", ID_SCHEMA, OpenApiParameter.PATH),
    OpenApiParameter("prescription_id", ID_SCHEMA, OpenApiParameter.PATH),
]


def read_session(request):
    """Recheck B01 without the legacy authentication failure revocation write."""
    parts = get_authorization_header(request).split()
    if len(parts) != 2 or parts[0].lower() != b"bearer":
        raise ExpiryError("authentication_required")
    try:
        parsed = parse_token(parts[1].decode("ascii"))
    except UnicodeError:
        parsed = None
    if parsed is None:
        raise ExpiryError("authentication_required")
    token_id, secret = parsed
    token = (
        AuthToken.objects.filter(pk=token_id, kind="access")
        .select_related("session__user")
        .first()
    )
    now = timezone.now()
    if token is None or not verify_secret(secret, token.token_hash):
        raise ExpiryError("authentication_required")
    session, user = token.session, token.session.user
    if (
        token.revoked_at
        or token.expires_at <= now
        or session.revoked_at
        or session.expires_at <= now
        or not user.is_active
        or session.password_fingerprint != fingerprint(user)
    ):
        raise ExpiryError("authentication_required")
    return user, token


def authorized_rows(user_id):
    nurse = Nurse.objects.filter(user_id=user_id).values_list("pk", flat=True).first()
    if nurse is None:
        raise ExpiryError("not_found")
    rows = Prescription.objects.filter(patient__nurse__pk=nurse)
    versions = (
        PrescriptionDocumentVersion.objects.select_related("document_import")
        .order_by("-version")
        .prefetch_related(
            Prefetch(
                "assets",
                queryset=PrescriptionDocumentAsset.objects.order_by("pk")[:41],
                to_attr="expiry_assets",
            )
        )[:1]
    )
    return (
        rows.select_related("patient")
        .only(
            "id",
            "patient_id",
            "prescribing_doctor",
            "start_date",
            "end_date",
            "photo_prescription",
            "patient__id",
            "patient__firstname",
            "patient__lastname",
        )
        .annotate(
            expiry_revision=Subquery(
                ExpiryPrescriptionState.objects.filter(
                    prescription_id=OuterRef("pk")
                ).values("revision")[:1]
            )
        )
        .prefetch_related(
            Prefetch(
                "document_versions",
                queryset=versions,
                to_attr="expiry_document_versions",
            )
        )
    )


def filtered_rows(rows, name, day):
    complete = Q(start_date__isnull=False, end_date__isnull=False)
    invalid = complete & Q(start_date__gt=F("end_date"))
    valid_dates = complete & Q(start_date__lte=F("end_date"))
    valid_today = valid_dates & Q(start_date__lte=day, end_date__gte=day)
    filters = {
        "all": Q(),
        "to_renew": valid_today & Q(end_date__lte=day + timedelta(days=10)),
        "expired": valid_dates & Q(end_date__lt=day),
        "unknown": Q(start_date__isnull=True) | Q(end_date__isnull=True),
        "future": valid_dates & Q(start_date__gt=day),
        "invalid": invalid,
        "renewed": Q(pk__in=[]),
    }
    return rows.filter(filters[name])


def source_exists(key):
    try:
        return default_storage.exists(key)
    except Exception:
        raise ExpiryError("service_unavailable") from None


def document_version(prescription):
    """Bounded metadata and existence proof; v3 still controls actual content."""
    versions = prescription.expiry_document_versions
    version = versions[0] if versions else None
    if version is None:
        field = prescription.photo_prescription
        return 1 if field.name and source_exists(field.name) else None
    imported = version.document_import
    if (
        imported.state != "confirmed"
        or imported.prescription_id != prescription.pk
        or imported.patient_id != prescription.patient_id
        or version.version < 1
    ):
        return None
    if not isinstance(imported.metadata, dict):
        return None
    pages = imported.metadata.get("pages")
    assets = version.expiry_assets
    if (
        not isinstance(pages, list)
        or not 1 <= len(pages) <= 20
        or not 1 <= len(assets) <= 40
    ):
        return None
    originals = {a.original_index: a for a in assets if a.role == "original"}
    if not originals or any(type(index) is not int or index < 0 for index in originals):
        return None
    original_ids = {asset.pk for asset in originals.values()}
    for asset in assets:
        if asset.role == "original":
            if asset.source_asset_id is not None:
                return None
        elif asset.role != "derived" or asset.source_asset_id not in original_ids:
            return None
        if (
            asset.document_import_id != imported.pk
            or asset.mime_type not in ("image/jpeg", "image/png", "application/pdf")
            or not 1 <= asset.byte_size <= 20 * 1024 * 1024
            or not 1 <= asset.page_count <= 20
            or not re.fullmatch(r"[a-f0-9]{64}", asset.sha256)
            or not asset.storage_key
            or not source_exists(asset.storage_key)
        ):
            return None
    for page in pages:
        if not isinstance(page, dict):
            return None
        index = page.get("asset_index")
        if type(index) is not int:
            return None
        source = originals.get(index)
        source_page = page.get("source_page")
        if (
            source is None
            or type(source_page) is not int
            or not 1 <= source_page <= source.page_count
            or page.get("rotation") not in (0, 90, 180, 270)
        ):
            return None
    return version.version


def item_data(row, day):
    if row.expiry_revision is None:
        raise ExpiryError("service_unavailable")
    if (
        not 1 <= row.pk <= 9007199254740991
        or not 1 <= row.patient_id <= 9007199254740991
    ):
        raise ExpiryError("service_unavailable")
    return {
        "id": row.pk,
        "patient": {
            "id": row.patient_id,
            "firstname": row.patient.firstname or "",
            "lastname": row.patient.lastname or "",
        },
        "prescribing_doctor": row.prescribing_doctor,
        "start_date": row.start_date.isoformat() if row.start_date else None,
        "end_date": row.end_date.isoformat() if row.end_date else None,
        "revision": str(row.expiry_revision),
        **classify_dates(row.start_date, row.end_date, day),
        "followup_state": "to_process",
        "replacement": None,
        "document_version": document_version(row),
    }


class ExpiryReadView(PrivateReadView):
    schema = ExpirySchema()

    def perform_authentication(self, request):
        user, token = read_session(request)
        request.user, request.auth = user, (token.session, token)

    def handle_exception(self, exc):
        if isinstance(exc, ExpiryError):
            error = exc
        elif isinstance(exc, (AuthenticationFailed, NotAuthenticated)):
            error = ExpiryError("authentication_required")
        elif isinstance(exc, (NotFound, Http404)):
            error = ExpiryError("not_found")
        elif isinstance(exc, (ValidationError, BadRequest, SuspiciousOperation)):
            error = ExpiryError("invalid_request")
        elif isinstance(exc, MethodNotAllowed):
            error = ExpiryError("method_not_allowed")
        elif isinstance(exc, UnsupportedMediaType):
            error = ExpiryError("unsupported_media_type")
        elif isinstance(exc, Throttled):
            error = ExpiryError("rate_limited")
        elif isinstance(exc, (DatabaseError, OSError)):
            error = ExpiryError("service_unavailable")
        else:
            error = ExpiryError("internal_error")
        response = Response(error.body, status=error.status)
        if error.status == 401:
            response["WWW-Authenticate"] = "Bearer"
        if isinstance(exc, Throttled):
            response["Retry-After"] = str(max(0, math.ceil(exc.wait or 0)))
        return response

    def query(self, request):
        query = request.query_params
        allowed = ExpiryQuerySerializer().fields if not self.kwargs else {}
        if any(key not in allowed or len(query.getlist(key)) != 1 for key in query):
            raise ExpiryError("invalid_request")
        if not allowed:
            return {}
        serializer = ExpiryQuerySerializer(data={key: query[key] for key in query})
        if not serializer.is_valid():
            if "cursor" in serializer.errors:
                raise ExpiryError("invalid_cursor")
            raise ExpiryError("invalid_request")
        return serializer.validated_data

    def begin(self, request):
        started = timezone.now()
        state = authority(request.user.pk)
        context = read_context(started, state)
        return state, context

    def finish(self, request, state, context):
        user, token = read_session(request)
        session, original_token = request.auth
        if (
            user.pk != request.user.pk
            or token.session_id != session.pk
            or token.pk != original_token.pk
        ):
            raise ExpiryError("authentication_required")
        finish_read(request.user.pk, state, context)

    def options(self, request, **kwargs):
        self.query(request)
        state, context = self.begin(request)
        if kwargs:
            self.detail_row(request, kwargs)
        self.finish(request, state, context)
        return Response({"allowed_methods": "GET, HEAD, OPTIONS"})

    def detail_row(self, request, kwargs):
        ids = {}
        for field in ("patient_id", "prescription_id"):
            value = kwargs[field]
            if (
                not re.fullmatch(r"[1-9][0-9]{0,15}", value)
                or int(value) > 9007199254740991
            ):
                raise ExpiryError("not_found")
            ids[field] = int(value)
        row = (
            authorized_rows(request.user.pk)
            .filter(patient_id=ids["patient_id"], pk=ids["prescription_id"])
            .first()
        )
        if row is None:
            raise ExpiryError("not_found")
        return row


class ExpiryPrescriptionList(ExpiryReadView):
    @extend_schema(
        operation_id="v4_expiry_prescriptions_list",
        parameters=QUERY_PARAMETERS,
        responses={200: ExpiryPageSerializer, **ERROR_RESPONSES},
        description="Bearer B01. Lecture privée du compte entier. "
        "Filtres avant pagination. "
        "Tout changement de portée, collection, règle ou jour invalide la pagination. "
        "HEAD et OPTIONS authentifiés ; aucune mutation. private, no-store.",
    )
    def get(self, request):
        query = self.query(request)
        state, context = self.begin(request)
        bound = cursor_context(request.user.pk, query, context)
        offset = cursor_offset(query.get("cursor"), bound)
        day = date.fromisoformat(context["business_date"])
        rows = authorized_rows(request.user.pk)
        if rows.filter(expiry_revision__isnull=True).exists():
            raise ExpiryError("service_unavailable")
        rows = filtered_rows(rows, query["filter"], day)
        ordering = {
            "end_date_asc": (F("end_date").asc(nulls_last=True), "id"),
            "end_date_desc": (F("end_date").desc(nulls_last=True), "-id"),
        }
        rows = rows.order_by(*ordering[query["ordering"]])
        count = rows.count()
        results = [
            item_data(row, day) for row in rows[offset : offset + query["page_size"]]
        ]
        following = offset + len(results)
        cursor = next_cursor(bound, following) if following < count else None
        self.finish(request, state, context)
        return Response(
            {
                "contract_revision": "0.2-decision",
                "context": context,
                "filter": query["filter"],
                "ordering": query["ordering"],
                "page_size": query["page_size"],
                "count": count,
                "next_cursor": cursor,
                "results": results,
            }
        )


class ExpiryPrescriptionDetail(ExpiryReadView):
    @extend_schema(
        operation_id="v4_expiry_prescriptions_retrieve",
        parameters=DETAIL_PARAMETERS,
        responses={200: ExpiryDetailSerializer, **ERROR_RESPONSES},
        description="Droits courants Nurse→patient→ordonnance, sans bypass staff. "
        "Capacités mutation fermées. Aucun paramètre query. private, no-store.",
    )
    def get(self, request, **kwargs):
        # Object access precedes diagnostics of query or revision.
        row = self.detail_row(request, kwargs)
        self.query(request)
        state, context = self.begin(request)
        # Re-read inside the observed generation, not the authorization precheck.
        row = self.detail_row(request, kwargs)
        item = item_data(row, date.fromisoformat(context["business_date"]))
        self.finish(request, state, context)
        return Response(
            {
                "contract_revision": "0.2-decision",
                "context": context,
                "item": item,
                "can_correct_dates": False,
                "can_confirm_renewal": False,
            }
        )
