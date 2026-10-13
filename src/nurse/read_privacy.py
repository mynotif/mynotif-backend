"""Suppress sensitive diagnostics for v2 reads, before logging/Sentry sinks."""

import logging
from contextvars import ContextVar
from urllib.parse import urlsplit

_read_route = ContextVar("private_read_route", default=None)
_RECORD_KEYS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


def read_route(path):
    if not isinstance(path, str):
        return None
    path = urlsplit(path).path
    if path == "/api/v2/patients/":
        return "/api/v2/patients/"
    if path.startswith("/api/v2/patients/"):
        parts = path.rstrip("/").split("/")
        if len(parts) == 6 and parts[5] == "prescriptions":
            return "/api/v2/patients/{patient_id}/prescriptions/"
        if len(parts) == 7 and parts[5] == "prescriptions":
            return "/api/v2/patients/{patient_id}/prescriptions/{prescription_id}/"
        return "/api/v2/patients/{resource}/"
    return None


def _record_route(record):
    route = _read_route.get()
    request = getattr(record, "request", None)
    if request is not None:
        route = route or read_route(getattr(request, "path", ""))
    if route:
        return route
    # Django's dev server logs after the middleware context has been reset.
    candidates = [record.msg]
    args = record.args.values() if isinstance(record.args, dict) else record.args
    candidates.extend(args or ())
    for value in candidates:
        if isinstance(value, str) and "/api/v2/patients/" in value:
            start = value.index("/api/v2/patients/")
            return read_route(value[start:].split()[0])
    return None


def redact_record(record):
    route = _record_route(record)
    if route:
        record.msg = "Private read %s"
        record.args = (route,)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        # Retain only standard metadata and a numeric HTTP status. No arbitrary
        # structured extras, request object, SQL arguments or exception body.
        for key in set(record.__dict__) - _RECORD_KEYS:
            if key != "status_code" or not isinstance(record.__dict__[key], int):
                record.__dict__.pop(key, None)
    return record


def install_log_redaction():
    previous = logging.Logger.makeRecord
    if getattr(previous, "_private_reads", False):
        return

    def make_record(logger, *args, **kwargs):
        # makeRecord has attached `extra` before returning, unlike the factory.
        return redact_record(previous(logger, *args, **kwargs))

    make_record._private_reads = True
    logging.Logger.makeRecord = make_record


def _private_event(event):
    if _read_route.get():
        return True
    request = event.get("request") or {}
    return bool(read_route(request.get("url", "")))


def before_send(event, hint):
    return None if _private_event(event) else event


def before_breadcrumb(crumb, hint):
    if _read_route.get():
        return None
    data = crumb.get("data") or {}
    if read_route(data.get("url", "")):
        return None
    message = crumb.get("message", "")
    if isinstance(message, str) and "/api/v2/patients/" in message:
        return None
    return crumb


class PrivateReadPrivacyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        route = read_route(request.path)
        if not route:
            return self.get_response(request)
        token = _read_route.set(route)
        try:
            response = self.get_response(request)
            if response.status_code == 500:
                # Preserve 500 (outside the stable contract table), but never
                # expose Django's DEBUG page or an unexpected exception body.
                from rest_framework.exceptions import server_error

                generic = server_error(request)
                response.content = b"" if request.method == "HEAD" else generic.content
                response["Content-Type"] = generic["Content-Type"]
                response.headers.pop("Content-Length", None)
            response["Cache-Control"] = "private, no-store"
            response["Pragma"] = "no-cache"
            response.headers.pop("ETag", None)
            return response
        finally:
            _read_route.reset(token)
