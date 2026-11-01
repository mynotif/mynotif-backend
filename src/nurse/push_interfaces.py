"""Closed B07 boundaries. No sender, event reservation, or proof double here."""

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit


class RegistryError(Exception):
    def __init__(self, code="registry_unavailable", status=503):
        self.code, self.status = code, status
        super().__init__(code)


class ControlDelivery(Protocol):
    def deliver(self, *, enrollment_id, nonce, namespace, envelope, aad):
        """One non-visible control delivery; never retry or return its nonce."""
        ...


@dataclass(frozen=True)
class BarrierResult:
    confirmed: bool = False
    in_flight: str = "unknown_or_present"


class RevocationBarrier(Protocol):
    def confirm(self, *, binding_id, epoch, generation) -> BarrierResult: ...


class ClosedBarrier:
    def confirm(self, **kwargs):
        return BarrierResult()


def revocation_result(binding, evidence):
    """Only explicit evidence from the serialized egress can produce an ACK."""
    if not isinstance(evidence, BarrierResult) or not evidence.confirmed:
        return {
            "state": "revocation_barrier_unconfirmed",
            "database_revoked": True,
        }, 503
    if evidence.in_flight not in ("none", "unknown_or_present"):
        return {
            "state": "revocation_barrier_unconfirmed",
            "database_revoked": True,
        }, 503
    return {
        "binding_id": str(binding.pk),
        "state": "revoked_for_new_submissions",
        "database_revoked": True,
        "barrier_confirmed": True,
        "in_flight": evidence.in_flight,
    }, 200 if evidence.in_flight == "none" else 202


@dataclass(frozen=True)
class SelectedRoute:
    device_ref: str
    namespace: str
    binding_id: str
    epoch: int
    generation: int
    proven: bool


def unambiguous_routes(selected):
    """B06 port: only explicit, proven single-channel choices; empty stays empty.

    device_ref is an already qualified mapping supplied by B06/Web/C05, not an
    authority provided by a client. B07 does not infer physical-device identity.
    """
    routes = {}
    for route in selected:
        routes.setdefault(route.device_ref, []).append(route)
    return tuple(
        values[0] for values in routes.values() if len(values) == 1 and values[0].proven
    )


def snapshot_replace_allowed(expected, current, *, absence_of_admission_proven):
    return expected == current and absence_of_admission_proven is True


def summarize_results(results):
    counts = {name: 0 for name in ("accepted", "refused", "uncertain", "cancelled")}
    for result in results:
        if result not in counts:
            raise ValueError("invalid_result")
        counts[result] += 1
    return {"counts": counts, "partial": sum(bool(n) for n in counts.values()) > 1}


@contextmanager
def private_push():
    # Reuse the repository-wide LogRecord scrubber, including SQL and extras.
    from nurse.read_privacy import _read_route, install_log_redaction

    install_log_redaction()
    install_push_log_redaction()
    marker = _read_route.set("/api/v2/push/")
    try:
        yield
    finally:
        _read_route.reset(marker)


def is_push_url(value):
    return isinstance(value, str) and urlsplit(value).path.startswith("/api/v2/push/")


def install_push_log_redaction():
    """Also scrub server access/error logs emitted after middleware unwinds."""
    previous = logging.Logger.makeRecord
    if getattr(previous, "_b07_privacy", False):
        return

    def make_record(logger, *args, **kwargs):
        from nurse.read_privacy import _read_route, redact_record

        record = previous(logger, *args, **kwargs)
        values = [record.msg, getattr(getattr(record, "request", None), "path", "")]
        values.extend(
            record.args.values() if isinstance(record.args, dict) else record.args or ()
        )
        if any(isinstance(value, str) and "/api/v2/push/" in value for value in values):
            marker = _read_route.set("/api/v2/push/")
            try:
                redact_record(record)
            finally:
                _read_route.reset(marker)
        return record

    make_record._b07_privacy = True
    # Preserve the existing installer's marker to avoid wrapping on every call.
    make_record._private_reads = True
    logging.Logger.makeRecord = make_record


def before_send(event, hint):
    from nurse.read_privacy import before_send as existing

    if is_push_url((event.get("request") or {}).get("url", "")):
        return None
    return existing(event, hint)


def before_breadcrumb(crumb, hint):
    from nurse.read_privacy import before_breadcrumb as existing

    if is_push_url((crumb.get("data") or {}).get("url", "")):
        return None
    if "/api/v2/push/" in str(crumb.get("message", "")):
        return None
    return existing(crumb, hint)


class PushPrivacyMiddleware:
    def __init__(self, get_response):
        install_push_log_redaction()
        self.get_response = get_response

    def __call__(self, request):
        if not is_push_url(request.path):
            return self.get_response(request)
        with private_push():
            response = self.get_response(request)
            if response.status_code >= 500 and not getattr(
                response, "_b07_safe_error", False
            ):
                from django.http import JsonResponse

                response = JsonResponse(
                    {"code": "registry_unavailable"}, status=response.status_code
                )
            response["Cache-Control"] = "private, no-store"
            response["Pragma"] = "no-cache"
            response.headers.pop("ETag", None)
            return response
