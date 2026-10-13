import logging

import pytest
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import resolve
from rest_framework.test import APIClient

from nurse.read_privacy import (
    PrivateReadPrivacyMiddleware,
    before_breadcrumb,
    before_send,
    read_route,
)
from nurse.read_views import PatientReadList

MARKER = "SYNTHETIC_M03_SEARCH_7349"


def test_routes_redact_identifiers():
    assert read_route("/api/v2/patients/?search=" + MARKER) == "/api/v2/patients/"
    assert (
        read_route("/api/v2/patients/913579/prescriptions/?search=" + MARKER)
        == "/api/v2/patients/{patient_id}/prescriptions/"
    )
    assert (
        read_route("/api/v2/patients/913579/prescriptions/824680/")
        == "/api/v2/patients/{patient_id}/prescriptions/{prescription_id}/"
    )
    assert read_route("/api/v1/patient/?search=" + MARKER) is None
    assert read_route("/api/v3/patients/913579/document-imports/abc/") == (
        "/api/v3/private-document/"
    )


def test_request_and_dev_server_logs_are_redacted(caplog):
    caplog.set_level(logging.DEBUG)
    path = "/api/v2/patients/913579/prescriptions/824680/?search=" + MARKER
    request = RequestFactory().get(path)
    for logger in ("django.request", "django.server"):
        logging.getLogger(logger).warning(
            "Synthetic GET %s", path, extra={"request": request}
        )
    assert MARKER not in caplog.text
    assert "913579" not in caplog.text and "824680" not in caplog.text
    assert "{patient_id}" in caplog.text and "{prescription_id}" in caplog.text
    assert all(getattr(record, "request", None) is None for record in caplog.records)

    v3_path = "/api/v3/patients/913579/document-imports/824680/?secret=" + MARKER
    v3_request = RequestFactory().get(v3_path)
    for logger in ("django.request", "django.server"):
        logging.getLogger(logger).warning(
            "Synthetic GET %s", v3_path, extra={"request": v3_request}
        )
    assert MARKER not in caplog.text
    assert "913579" not in caplog.text and "824680" not in caplog.text


def test_context_sanitizes_sql_exception_tokens_body_and_sentry(caplog):
    caplog.set_level(logging.DEBUG)
    request = RequestFactory().get("/api/v2/patients/?search=" + MARKER)

    def response(req):
        logging.getLogger("django.db.backends").debug("SQL search=%s", MARKER)
        logging.getLogger("synthetic.structured").warning(
            "Sensitive extras",
            extra={
                "search": MARKER,
                "token": "SYNTHETIC_TOKEN",
                "patient_id": 913579,
                "prescription_id": 824680,
                "body": MARKER,
                "request": req,
            },
        )
        try:
            raise ValueError("Synthetic body " + MARKER)
        except ValueError:
            logging.getLogger("synthetic.provider").exception(
                "Bearer SYNTHETIC_TOKEN " + MARKER
            )
        assert (
            before_send(
                {"request": {"url": "https://test.invalid/"}, "user": {"id": 913579}},
                {},
            )
            is None
        )
        assert before_breadcrumb({"message": MARKER}, {}) is None
        return HttpResponse(status=200)

    PrivateReadPrivacyMiddleware(response)(request)
    assert MARKER not in caplog.text and "SYNTHETIC_TOKEN" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    for record in caplog.records:
        assert (
            not {"search", "token", "patient_id", "prescription_id", "body", "request"}
            & record.__dict__.keys()
        )
    logging.getLogger("synthetic.after").warning("Safe diagnostic preserved")
    assert "Safe diagnostic preserved" in caplog.text


def test_sentry_events_transactions_breadcrumbs_scoped_to_reads():
    event = {
        "request": {"url": "https://test.invalid/api/v2/patients/?search=" + MARKER}
    }
    assert before_send(event, {}) is None
    assert before_breadcrumb({"data": {"url": event["request"]["url"]}}, {}) is None
    assert before_breadcrumb({"message": "GET " + event["request"]["url"]}, {}) is None
    ordinary = {"request": {"url": "https://test.invalid/api/v1/version/"}}
    assert before_send(ordinary, {}) is ordinary
    assert before_breadcrumb(ordinary, {}) is ordinary
    private_v3 = {
        "request": {
            "url": "https://test.invalid/api/v3/patients/913579/document-imports/abc/"
        }
    }
    assert before_send(private_v3, {}) is None
    assert (
        before_breadcrumb({"message": "GET " + private_v3["request"]["url"]}, {})
        is None
    )


@pytest.mark.django_db
def test_actual_unauthorized_read_does_not_log_search(caplog):
    caplog.set_level(logging.DEBUG)
    result = APIClient().get("/api/v2/patients/?search=" + MARKER)
    assert result.status_code == 401
    assert MARKER not in caplog.text
    assert MARKER not in result.content.decode()
    assert resolve("/api/v2/patients/").func.view_class is PatientReadList


def test_gunicorn_access_format_omits_sensitive_atoms():
    import runpy
    from pathlib import Path

    config = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "gunicorn.conf.py")
    )
    assert config["access_log_format"] == "%(t)s %(m)s %(s)s %(L)s"


def test_runtime_commands_explicitly_load_private_gunicorn_config():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    docker = (root / "Dockerfile").read_text()
    assert "COPY gunicorn.conf.py /app/gunicorn.conf.py" in docker
    assert "--config /app/gunicorn.conf.py" in docker
    assert "--config gunicorn.conf.py" in (root / "Makefile").read_text()
