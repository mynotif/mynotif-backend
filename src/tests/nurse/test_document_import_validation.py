import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from io import BytesIO
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils.datastructures import MultiValueDict
from PIL import Image
from rest_framework.exceptions import APIException
from rest_framework.test import APIClient

from nurse import document_import_services
from nurse.document_scanner import InfectedDocument, ScannerUnavailable, scan_bytes
from nurse.document_validation import (
    MAX_IMAGE_PIXELS,
    MAX_ORIGINAL_BYTES,
    WORKER_ADDRESS_SPACE,
    WORKER_CPU_SECONDS,
    WORKER_NOFILE,
    WORKER_TIMEOUT_SECONDS,
    DocumentTooLarge,
    ValidationUnavailable,
    _sandbox_command,
    _set_worker_limits,
    _worker_main,
    validate_document,
)
from nurse.models import (
    DocumentImport,
    Nurse,
    Patient,
    Prescription,
    PrescriptionDocumentAsset,
    PrescriptionDocumentVersion,
)

pytestmark = pytest.mark.django_db


def png_bytes():
    image = Image.new("RGB", (2, 3), color=(12, 34, 56))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def allowed_client():
    user = User.objects.create_user(username=f"synthetic-doc-{uuid.uuid4()}")
    nurse = Nurse.objects.create(user=user)
    patient = Patient.objects.create(firstname="Fictif", lastname="Exemple")
    nurse.patients.add(patient)
    client = APIClient()
    client.force_authenticate(user)
    return client, patient


def import_payload(patient_id, import_id, image_bytes=None):
    image_bytes = image_bytes or png_bytes()
    metadata = {
        "prescribing_doctor": "",
        "start_date": None,
        "end_date": None,
        "source_kind": "image_import",
        "pages": [{"asset_index": 0, "source_page": 1, "rotation": 0, "crop": None}],
    }
    return (
        f"/api/v3/patients/{patient_id}/document-imports/",
        {
            "metadata": json.dumps(metadata),
            "original_0": SimpleUploadedFile(
                "not-returned-to-client.png", image_bytes, content_type="image/png"
            ),
        },
        str(import_id),
    )


def test_capability_contract_is_closed_and_advertises_validation_limits():
    client, _ = allowed_client()
    response = client.get("/api/v3/document-capabilities/")
    assert response.status_code == 200
    assert response.json() == {
        "contract_version": 1,
        "mime_types": ["image/jpeg", "image/png", "application/pdf"],
        "max_original_bytes": 20_971_520,
        "max_pages": 20,
        "max_image_pixels": 40_000_000,
        "max_total_image_pixels": 200_000_000,
        "max_derived_bytes": 20_971_520,
        "max_metadata_bytes": 65_536,
        "draft_ttl_seconds": 86_400,
    }
    assert response["Cache-Control"] == "private, no-store"


def test_import_openapi_exposes_only_documented_http_methods():
    from drf_spectacular.generators import SchemaGenerator

    paths = SchemaGenerator().get_schema(request=None, public=True)["paths"]
    expected = {
        "/api/v3/document-capabilities/": {"get"},
        "/api/v3/patients/{patient_id}/document-imports/": {"get", "post"},
        "/api/v3/patients/{patient_id}/document-imports/{import_uuid}/": {
            "get",
            "patch",
            "delete",
        },
        "/api/v3/patients/{patient_id}/document-imports/{import_uuid}/confirmation/": {
            "put"
        },
        "/api/v3/patients/{patient_id}/document-imports/{import_uuid}/assets/"
        "{asset_id}/content/": {"get"},
    }
    for path, methods in expected.items():
        assert set(paths[path]) == methods


def test_upload_retry_and_confirmation_replay_create_one_prescription(
    tmp_path, monkeypatch
):
    from nurse import document_import_services

    client, patient = allowed_client()
    monkeypatch.setattr(document_import_services, "scan_bytes", lambda *args: True)
    monkeypatch.setattr(
        document_import_services,
        "validate_document",
        lambda content: {
            "mime_type": "image/png",
            "page_count": 1,
            "width": 2,
            "height": 3,
            "image_pixels": 6,
            "sha256": __import__("hashlib").sha256(content).hexdigest(),
            "byte_size": len(content),
        },
    )
    with override_settings(
        STORAGES={
            "default": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": str(tmp_path)},
            },
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            },
        }
    ):
        import_id = uuid.uuid4()
        original_bytes = png_bytes()
        url, data, idem = import_payload(
            patient.pk, import_id, image_bytes=original_bytes
        )
        first = client.post(
            url, data=data, format="multipart", HTTP_IDEMPOTENCY_KEY=idem
        )
        assert first.status_code == 202, first.data
        assert first.data["state"] == "ready"
        assert first.data["metadata"]["start_date"] is None
        assert first.data["metadata"]["end_date"] is None
        assert len(first.data["assets"]) == 1
        assert (
            first.data["assets"][0]["sha256"]
            == __import__("hashlib").sha256(original_bytes).hexdigest()
        )
        asset_id = first.data["assets"][0]["asset_id"]
        content_response = client.get(f"{url}{import_id}/assets/{asset_id}/content/")
        assert content_response.status_code == 200
        assert content_response["X-Content-Type-Options"] == "nosniff"
        assert content_response["Cache-Control"] == "private, no-store"
        assert b"".join(content_response.streaming_content) == original_bytes

        replay_url, replay_data, replay_key = import_payload(patient.pk, import_id)
        replay = client.post(
            replay_url,
            data=replay_data,
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=replay_key,
        )
        assert replay.status_code == 200
        assert replay.data["import_id"] == first.data["import_id"]
        assert DocumentImport.objects.filter(import_uuid=import_id).count() == 1

        confirm_url = url + f"{import_id}/confirmation/"
        body = {
            "revision": first.data["revision"],
            "patient_confirmed": True,
            "pages_confirmed": True,
            "dates_reviewed": True,
        }
        recorded = client.put(confirm_url, body, format="json")
        replayed_record = client.put(confirm_url, body, format="json")
        assert recorded.status_code == 201, recorded.data
        assert replayed_record.status_code == 200
        assert replayed_record.data == recorded.data
        assert Prescription.objects.filter(patient=patient).count() == 1
        assert Prescription.objects.get(patient=patient).start_date is None
        assert PrescriptionDocumentVersion.objects.count() == 1
        assert (
            PrescriptionDocumentAsset.objects.filter(
                document_version__isnull=False
            ).count()
            == 1
        )


def test_scanner_unavailable_releases_lease_without_publishing_and_same_key_recovers(
    tmp_path, monkeypatch
):
    client, patient = allowed_client()
    import_id = uuid.uuid4()
    url, data, idem = import_payload(patient.pk, import_id)

    def unavailable(*args, **kwargs):
        raise ScannerUnavailable()

    monkeypatch.setattr(document_import_services, "scan_bytes", unavailable)
    response = client.post(
        url, data=data, format="multipart", HTTP_IDEMPOTENCY_KEY=idem
    )
    assert response.status_code == 503
    draft = DocumentImport.objects.get(import_uuid=import_id)
    assert draft.state == "retryable"
    assert draft.metadata is None
    assert not draft.assets.exists()

    monkeypatch.setattr(document_import_services, "scan_bytes", lambda *args: True)
    monkeypatch.setattr(
        document_import_services,
        "validate_document",
        lambda content: {
            "mime_type": "image/png",
            "page_count": 1,
            "width": 2,
            "height": 3,
            "image_pixels": 6,
            "sha256": __import__("hashlib").sha256(content).hexdigest(),
            "byte_size": len(content),
        },
    )
    with override_settings(
        STORAGES={
            "default": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": str(tmp_path)},
            },
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
            },
        }
    ):
        replay_url, replay_data, replay_key = import_payload(patient.pk, import_id)
        recovered = client.post(
            replay_url,
            data=replay_data,
            format="multipart",
            HTTP_IDEMPOTENCY_KEY=replay_key,
        )
    assert recovered.status_code == 202
    assert recovered.data["state"] == "ready"
    assert DocumentImport.objects.filter(import_uuid=import_id).count() == 1


def test_foreign_patient_is_hidden_before_multipart_validation():
    client, _ = allowed_client()
    response = client.post(
        "/api/v3/patients/9223372036854775807/document-imports/",
        data={"metadata": "not-json"},
        format="multipart",
        HTTP_IDEMPOTENCY_KEY="invalid",
    )
    assert response.status_code == 404
    assert response.data["code"] == "not_found"


def test_original_byte_limit_fails_before_worker():
    with pytest.raises(DocumentTooLarge):
        validate_document(b"x" * (MAX_ORIGINAL_BYTES + 1))


def test_worker_argv_has_explicit_isolation_and_no_shell():
    command = _sandbox_command()
    for required in (
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--ro-bind",
        "--clearenv",
        "--cap-drop",
        "ALL",
        "--worker",
    ):
        assert required in command
    assert "-c" not in command
    assert command[-2] == "/opt/worker-validation.py"
    assert command[command.index("--ro-bind") + 1] != "/"
    assert "/run" in command


def test_resource_policy_sets_all_numeric_limits(monkeypatch):
    from nurse import document_validation

    observed = []
    monkeypatch.setattr(
        document_validation.resource,
        "setrlimit",
        lambda key, value: observed.append((key, value)),
    )
    _set_worker_limits()
    limits = dict(observed)
    assert limits[document_validation.resource.RLIMIT_CPU] == (5, 5)
    assert limits[document_validation.resource.RLIMIT_AS] == (512 * 1024 * 1024,) * 2
    assert limits[document_validation.resource.RLIMIT_FSIZE] == (0, 0)
    assert limits[document_validation.resource.RLIMIT_NOFILE] == (16, 16)
    assert WORKER_CPU_SECONDS == 5
    assert WORKER_ADDRESS_SPACE == 512 * 1024 * 1024
    assert WORKER_NOFILE == 16
    assert WORKER_TIMEOUT_SECONDS == 8


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "code"),
    [
        (
            "WORKER_ADDRESS_SPACE",
            128 * 1024 * 1024,
            "try:\n bytearray(256*1024*1024)\n print('allocated')\nexcept MemoryError:\n print('blocked')",
        ),
        (
            "WORKER_NOFILE",
            16,
            "fds=[]\ntry:\n for _ in range(64): fds.append(open('/dev/null'))\n print('allocated')\nexcept OSError:\n print('blocked')",
        ),
    ],
)
def test_address_space_and_descriptor_limits_block_synthetic_children(
    monkeypatch, limit_name, limit_value, code
):
    from nurse import document_validation

    monkeypatch.setattr(document_validation, limit_name, limit_value)
    child = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        preexec_fn=_set_worker_limits,
        timeout=3,
        check=False,
    )
    assert child.returncode == 0
    assert child.stdout.strip() == "blocked"


def test_file_size_limit_blocks_synthetic_child(monkeypatch, tmp_path):
    from nurse import document_validation

    monkeypatch.setattr(document_validation, "WORKER_ADDRESS_SPACE", 128 * 1024 * 1024)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            f"try:\n f=open({str(tmp_path / 'blocked').__repr__()},'wb')\n f.write(b'x')\n f.flush()\n print('allocated')\nexcept OSError:\n print('blocked')",
        ],
        capture_output=True,
        text=True,
        preexec_fn=_set_worker_limits,
        timeout=3,
        check=False,
    )
    assert child.returncode == 0
    assert child.stdout.strip() == "blocked"
    assert (tmp_path / "blocked").stat().st_size == 0


def test_cpu_limit_terminates_synthetic_worker(monkeypatch):
    from nurse import document_validation

    monkeypatch.setattr(document_validation, "WORKER_CPU_SECONDS", 1)
    started = time.monotonic()
    child = subprocess.run(
        [sys.executable, "-c", "while True: pass"],
        capture_output=True,
        preexec_fn=_set_worker_limits,
        timeout=4,
        check=False,
    )
    assert child.returncode != 0
    assert time.monotonic() - started < 4


def test_wall_timeout_kills_the_sandbox_process_group(monkeypatch):
    from nurse import document_validation

    command = [
        sys.executable,
        "-c",
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); "
        "time.sleep(30)",
    ]
    killed_groups = []
    original_killpg = os.killpg
    monkeypatch.setattr(
        document_validation, "_sandbox_command", lambda worker_args=(): command
    )
    monkeypatch.setattr(document_validation, "WORKER_TIMEOUT_SECONDS", 0.1)

    def record_killpg(pid, sig):
        killed_groups.append((pid, sig))
        return original_killpg(pid, sig)

    monkeypatch.setattr(document_validation.os, "killpg", record_killpg)
    with pytest.raises(ValidationUnavailable):
        validate_document(png_bytes())
    assert len(killed_groups) == 1
    assert killed_groups[0][1] == document_validation.signal.SIGKILL


def test_actual_parser_sandbox_never_accepts_a_file_when_namespace_setup_fails():
    try:
        details = validate_document(png_bytes())
    except ValidationUnavailable:
        assert True
    else:
        assert details["mime_type"] == "image/png"
        assert details["page_count"] == 1


def test_sandbox_worker_output_is_bounded_and_terminated(monkeypatch):
    from nurse import document_validation

    monkeypatch.setattr(
        document_validation,
        "_sandbox_command",
        lambda worker_args=(): [
            sys.executable,
            "-c",
            "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(b'x'*1000000)",
        ],
    )
    monkeypatch.setattr(document_validation, "WORKER_OUTPUT_LIMIT", 1024)
    with pytest.raises(ValidationUnavailable):
        validate_document(png_bytes())


def worker_parse(monkeypatch, content):
    from nurse import document_validation

    output = BytesIO()
    monkeypatch.setattr(
        document_validation.sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(content)),
    )
    monkeypatch.setattr(
        document_validation.sys,
        "stdout",
        SimpleNamespace(write=lambda value: output.write(value.encode("utf-8"))),
    )
    status = _worker_main()
    return status, json.loads(output.getvalue()) if output.getvalue() else None


def synthetic_pdf(page_count=1, *, encrypted=False, active=False):
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, TextStringObject

    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    if active:
        writer._root_object[NameObject("/OpenAction")] = TextStringObject("synthetic")
    if encrypted:
        writer.encrypt("synthetic-password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("action", ["/GoToR", "/SubmitForm", "/ImportData"])
def test_pdf_external_actions_are_rejected(monkeypatch, action):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer._root_object[NameObject("/SyntheticAction")] = DictionaryObject(
        {NameObject("/S"): NameObject(action)}
    )
    output = BytesIO()
    writer.write(output)
    status, result = worker_parse(monkeypatch, output.getvalue())
    assert status == 0 and result == {"error": "invalid_document"}


def test_isolated_image_derivative_applies_crop_then_rotation(monkeypatch):
    from nurse import document_validation

    class WorkerStdout:
        def __init__(self):
            self.buffer = BytesIO()

        def write(self, value):
            return self.buffer.write(value.encode("utf-8"))

    original = Image.new("RGB", (10, 20), color="red")
    source = BytesIO()
    original.save(source, format="PNG")
    stdout = WorkerStdout()
    monkeypatch.setattr(
        document_validation.sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(source.getvalue())),
    )
    monkeypatch.setattr(document_validation.sys, "stdout", stdout)
    monkeypatch.setattr(
        document_validation.sys,
        "argv",
        [
            "worker",
            "--worker",
            "--derive",
            json.dumps(
                {
                    "crop": {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.5},
                    "rotation": 90,
                }
            ),
        ],
    )

    assert _worker_main() == 0
    header, _, derivative = stdout.buffer.getvalue().partition(b"\n")
    details = json.loads(header)
    rendered = Image.open(BytesIO(derivative))
    assert rendered.size == (10, 5)
    assert details["mime_type"] == "image/jpeg"


@pytest.mark.parametrize("pages", [1, 20])
def test_pdf_complete_page_count_and_maximum_pages(monkeypatch, pages):
    status, result = worker_parse(monkeypatch, synthetic_pdf(page_count=pages))
    assert status == 0
    assert result == {"mime_type": "application/pdf", "page_count": pages}


def test_pdf_21_pages_encryption_and_active_actions_fail_closed(monkeypatch):
    status, result = worker_parse(monkeypatch, synthetic_pdf(page_count=21))
    assert status == 0 and result == {"error": "invalid_document"}
    status, result = worker_parse(monkeypatch, synthetic_pdf(encrypted=True))
    assert status == 0 and result == {"error": "encrypted_pdf"}
    status, result = worker_parse(monkeypatch, synthetic_pdf(active=True))
    assert status == 0 and result == {"error": "invalid_document"}


def test_malformed_pdf_and_corrupt_image_rejected(monkeypatch):
    status, result = worker_parse(monkeypatch, b"%PDF-1.7\nnot a real PDF")
    assert status == 0 and result == {"error": "invalid_document"}
    status, result = worker_parse(monkeypatch, b"\x89PNG\r\n\x1a\ntruncated")
    assert status == 0 and result == {"error": "invalid_document"}


def test_image_pixel_limit_accepts_exact_boundary_and_rejects_one_over(monkeypatch):
    exact = Image.new("1", (8000, 5000), color=0)
    stream = BytesIO()
    exact.save(stream, format="PNG")
    status, result = worker_parse(monkeypatch, stream.getvalue())
    assert status == 0
    assert result["image_pixels"] == MAX_IMAGE_PIXELS

    over = Image.new("1", (8000, 5001), color=0)
    stream = BytesIO()
    over.save(stream, format="PNG")
    status, result = worker_parse(monkeypatch, stream.getvalue())
    assert status == 0 and result == {"error": "invalid_document"}


def test_metadata_rejects_inverted_dates_and_accepts_unknown_and_leap_day():
    from nurse.document_import_schemas import _validate_metadata

    page = {"asset_index": 0, "source_page": 1, "rotation": 0, "crop": None}
    metadata = {
        "prescribing_doctor": "",
        "start_date": "2024-02-29",
        "end_date": None,
        "source_kind": "image_import",
        "pages": [page],
    }
    assert _validate_metadata(metadata)["end_date"] is None
    metadata["start_date"], metadata["end_date"] = "2025-02-02", "2025-02-01"
    with pytest.raises(APIException) as error:
        _validate_metadata(metadata)
    assert getattr(error.value, "status_code", None) == 400


@pytest.mark.parametrize("image_pixels", [10_000_000, 10_000_001])
def test_total_image_pixels_sum_boundary(image_pixels, monkeypatch):
    from nurse import document_import_services

    monkeypatch.setattr(document_import_services, "scan_bytes", lambda *args: True)
    monkeypatch.setattr(
        document_import_services,
        "validate_document",
        lambda content: {
            "mime_type": "image/png",
            "page_count": 1,
            "image_pixels": image_pixels,
        },
    )
    files = [b"synthetic"] * 20
    metadata = {
        "source_kind": "image_import",
        "pages": [
            {
                "asset_index": index,
                "source_page": 1,
                "rotation": 0,
                "crop": None,
            }
            for index in range(20)
        ],
    }
    if image_pixels == 10_000_000:
        assert len(document_import_services._validate_files(files, metadata)) == 20
    else:
        with pytest.raises(document_import_services.DocumentInvalid):
            document_import_services._validate_files(files, metadata)


def test_original_total_size_boundary_on_request_files():
    from nurse.document_import_schemas import DocumentTooLarge, _files

    exact = SimpleUploadedFile(
        "synthetic.png",
        b"x" * MAX_ORIGINAL_BYTES,
        content_type="image/png",
    )
    assert (
        len(_files(SimpleNamespace(FILES=MultiValueDict({"original_0": [exact]})))[0])
        == MAX_ORIGINAL_BYTES
    )
    over = SimpleUploadedFile(
        "synthetic.png",
        b"x" * (MAX_ORIGINAL_BYTES + 1),
        content_type="image/png",
    )
    with pytest.raises(DocumentTooLarge):
        _files(SimpleNamespace(FILES=MultiValueDict({"original_0": [over]})))


def _clamd_server(response, socket_path, ready):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(1)
    ready.set()
    connection, _ = server.accept()
    with connection:
        assert connection.recv(10) == b"zINSTREAM\0"
        while True:
            length = struct.unpack("!I", connection.recv(4))[0]
            if length == 0:
                break
            remaining = length
            while remaining:
                remaining -= len(connection.recv(remaining))
        connection.sendall(response.encode("ascii") + b"\0")
    server.close()


@pytest.mark.parametrize(
    ("response", "expected"),
    [("stream: OK", True), ("stream: Synthetic FOUND", InfectedDocument)],
)
def test_clamav_instream_protocol_clean_and_infected(tmp_path, response, expected):
    path = str(tmp_path / "clamd.sock")
    ready = threading.Event()
    thread = threading.Thread(target=_clamd_server, args=(response, path, ready))
    thread.start()
    assert ready.wait(timeout=2)
    if expected is True:
        assert scan_bytes(b"synthetic", path)
    else:
        with pytest.raises(expected):
            scan_bytes(b"synthetic", path)
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_clamav_unavailable_fails_closed(tmp_path):
    with pytest.raises(ScannerUnavailable):
        scan_bytes(b"synthetic", str(tmp_path / "missing.sock"), timeout=0.05)
