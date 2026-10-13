"""Bounded file inspection. Untrusted parsers run only in a bubblewrap worker."""

import hashlib
import json
import os
import resource
import signal
import subprocess
import sys
import threading
from io import BytesIO
from pathlib import Path

from django.conf import settings
from rest_framework.exceptions import APIException

MAX_ORIGINAL_BYTES = 20 * 1024 * 1024
MAX_PAGES = 20
MAX_IMAGE_PIXELS = 40_000_000
MAX_TOTAL_IMAGE_PIXELS = 200_000_000
WORKER_TIMEOUT_SECONDS = 8
WORKER_CPU_SECONDS = 5
WORKER_ADDRESS_SPACE = 512 * 1024 * 1024
WORKER_NOFILE = 16
WORKER_OUTPUT_LIMIT = 64 * 1024
_WORKER_SLOTS = threading.BoundedSemaphore(2)


class DocumentInvalid(APIException):
    status_code = 400
    default_detail = {"code": "invalid_document"}
    default_code = "invalid_document"


class DocumentTooLarge(APIException):
    status_code = 413
    default_detail = {"code": "document_too_large"}
    default_code = "document_too_large"


class ValidationUnavailable(APIException):
    status_code = 503
    default_detail = {"code": "validation_unavailable"}
    default_code = "validation_unavailable"


def _set_worker_limits():
    resource.setrlimit(resource.RLIMIT_CPU, (WORKER_CPU_SECONDS, WORKER_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (WORKER_ADDRESS_SPACE, WORKER_ADDRESS_SPACE))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (WORKER_NOFILE, WORKER_NOFILE))


def _sandbox_command(worker_args=()):
    bwrap = getattr(settings, "DOCUMENT_BWRAP_PATH", "/usr/bin/bwrap")
    python = "/opt/worker-venv/bin/python"
    runtime_roots = ("/usr", "/lib", "/lib64")
    return [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--dir",
        "/opt",
        "--ro-bind",
        sys.prefix,
        "/opt/worker-venv",
        "--ro-bind",
        str(Path(__file__).resolve()),
        "/opt/worker-validation.py",
        *[part for root in runtime_roots for part in ("--ro-bind", root, root)],
        "--dir",
        "/run",
        "--tmpfs",
        "/run",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--clearenv",
        "--cap-drop",
        "ALL",
        python,
        "/opt/worker-validation.py",
        "--worker",
        *worker_args,
    ]


def _run_worker(content, *, worker_args=(), output_limit=WORKER_OUTPUT_LIMIT):
    if not _WORKER_SLOTS.acquire(blocking=False):
        raise ValidationUnavailable()
    try:
        return _run_worker_held(
            content, worker_args=worker_args, output_limit=output_limit
        )
    finally:
        _WORKER_SLOTS.release()


def _run_worker_held(content, *, worker_args=(), output_limit=WORKER_OUTPUT_LIMIT):
    try:
        process = subprocess.Popen(
            _sandbox_command(worker_args),
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env={},
            preexec_fn=_set_worker_limits,
            start_new_session=True,
        )
        stdout_chunks = []
        stdout_size = 0
        output_exceeded = threading.Event()

        def read_stdout():
            nonlocal stdout_size
            while True:
                chunk = process.stdout.read1(4096)
                if not chunk:
                    return
                if stdout_size + len(chunk) > output_limit:
                    output_exceeded.set()
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return
                stdout_chunks.append(chunk)
                stdout_size += len(chunk)

        def write_stdin():
            try:
                process.stdin.write(content)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        output_thread = threading.Thread(target=read_stdout, daemon=True)
        input_thread = threading.Thread(target=write_stdin, daemon=True)
        output_thread.start()
        input_thread.start()
        try:
            process.wait(timeout=WORKER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise ValidationUnavailable() from error
        finally:
            input_thread.join(timeout=1)
            output_thread.join(timeout=1)
        if output_exceeded.is_set() or output_thread.is_alive():
            raise ValidationUnavailable()
        stdout = b"".join(stdout_chunks)
    except (OSError, ValueError, PermissionError) as error:
        raise ValidationUnavailable() from error
    if process.returncode != 0:
        raise ValidationUnavailable()
    return stdout


def validate_document(content):
    """Return trusted type/page/dimension details or fail closed."""
    if not content or len(content) > MAX_ORIGINAL_BYTES:
        raise DocumentTooLarge()
    if content.startswith(b"%PDF-"):
        mime_type = "application/pdf"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        mime_type = "image/jpeg"
    else:
        raise DocumentInvalid()

    stdout = _run_worker(content)
    try:
        result = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationUnavailable() from error
    if result == {"error": "invalid_document"}:
        raise DocumentInvalid()
    if result == {"error": "encrypted_pdf"}:
        error = APIException({"code": "encrypted_pdf"})
        error.status_code = 400
        raise error
    if not isinstance(result, dict) or result.get("mime_type") != mime_type:
        raise ValidationUnavailable()
    result["sha256"] = hashlib.sha256(content).hexdigest()
    result["byte_size"] = len(content)
    return result


def derive_image(content, recipe):
    """Create a bounded server preview derivative without changing the original."""
    if not content.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        raise DocumentInvalid()
    argument = json.dumps(recipe, separators=(",", ":"), sort_keys=True)
    stdout = _run_worker(
        content,
        worker_args=("--derive", argument),
        output_limit=MAX_ORIGINAL_BYTES + 1024,
    )
    header, separator, derivative = stdout.partition(b"\n")
    if not separator or not derivative:
        raise ValidationUnavailable()
    try:
        details = json.loads(header)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationUnavailable() from error
    if (
        not isinstance(details, dict)
        or details.get("mime_type") != "image/jpeg"
        or details.get("byte_size") != len(derivative)
        or len(derivative) > MAX_ORIGINAL_BYTES
    ):
        raise ValidationUnavailable()
    details["sha256"] = hashlib.sha256(derivative).hexdigest()
    return derivative, details


def _check_pdf(reader):
    if reader.is_encrypted:
        raise ValueError("encrypted_pdf")
    root = reader.trailer.get("/Root")
    forbidden = {
        "/OpenAction",
        "/AA",
        "/JavaScript",
        "/JS",
        "/Launch",
        "/EmbeddedFiles",
        "/URI",
        "/FS",
        "/EF",
    }
    forbidden_actions = {
        "/GoToR",
        "/GoToE",
        "/SubmitForm",
        "/ImportData",
        "/Launch",
        "/JavaScript",
        "/Rendition",
        "/Movie",
        "/Sound",
        "/GoTo3DView",
    }
    seen = set()
    stack = [(root, 0)]
    object_count = 0
    while stack:
        value, depth = stack.pop()
        if depth > 24:
            raise ValueError("depth")
        try:
            value = value.get_object()
        except AttributeError:
            pass
        ref = getattr(value, "indirect_reference", None)
        key = (getattr(ref, "idnum", None), getattr(ref, "generation", None))
        if key != (None, None):
            if key in seen:
                continue
            seen.add(key)
            object_count += 1
            if object_count > 20_000:
                raise ValueError("objects")
        if hasattr(value, "items"):
            if hasattr(value, "get"):
                action = value.get("/S")
                try:
                    action = action.get_object()
                except AttributeError:
                    pass
                if str(action) in forbidden_actions:
                    raise ValueError("active")
            for name, child in value.items():
                if str(name) in forbidden:
                    raise ValueError("active")
                stack.append((child, depth + 1))
        elif isinstance(value, (list, tuple)):
            stack.extend((child, depth + 1) for child in value)


def _worker_main():
    content = sys.stdin.buffer.read(MAX_ORIGINAL_BYTES + 1)
    if not content or len(content) > MAX_ORIGINAL_BYTES:
        return 2
    try:
        if content.startswith(b"%PDF-"):
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content), strict=True)
            _check_pdf(reader)
            pages = len(reader.pages)
            if not 1 <= pages <= MAX_PAGES:
                sys.stdout.write('{"error":"invalid_document"}')
                return 0
            result = {"mime_type": "application/pdf", "page_count": pages}
        else:
            from PIL import Image, ImageFile, ImageOps

            Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
            ImageFile.LOAD_TRUNCATED_IMAGES = False
            image = Image.open(BytesIO(content))
            if (
                image.format not in {"JPEG", "PNG"}
                or getattr(image, "n_frames", 1) != 1
            ):
                sys.stdout.write('{"error":"invalid_document"}')
                return 0
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                sys.stdout.write('{"error":"invalid_document"}')
                return 0
            image.verify()
            image = Image.open(BytesIO(content))
            image.load()
            if "--derive" in sys.argv:
                recipe = json.loads(sys.argv[sys.argv.index("--derive") + 1])
                image = ImageOps.exif_transpose(image)
                width, height = image.size
                crop = recipe["crop"]
                if crop is not None:
                    import math

                    left = math.floor(crop["x"] * width)
                    top = math.floor(crop["y"] * height)
                    right = math.ceil((crop["x"] + crop["width"]) * width)
                    bottom = math.ceil((crop["y"] + crop["height"]) * height)
                    image = image.crop((left, top, right, bottom))
                if recipe["rotation"]:
                    image = image.rotate(-recipe["rotation"], expand=True)
                image = image.convert("RGB")
                output = BytesIO()
                image.save(output, format="JPEG", quality=92, optimize=True)
                derivative = output.getvalue()
                if not derivative or len(derivative) > MAX_ORIGINAL_BYTES:
                    sys.stdout.write('{"error":"invalid_document"}')
                    return 0
                header = json.dumps(
                    {
                        "mime_type": "image/jpeg",
                        "byte_size": len(derivative),
                        "width": image.width,
                        "height": image.height,
                        "image_pixels": image.width * image.height,
                    },
                    separators=(",", ":"),
                )
                sys.stdout.buffer.write(header.encode("utf-8") + b"\n" + derivative)
                return 0
            result = {
                "mime_type": "image/jpeg" if image.format == "JPEG" else "image/png",
                "page_count": 1,
                "width": width,
                "height": height,
                "image_pixels": width * height,
            }
    except Exception as error:
        code = "encrypted_pdf" if str(error) == "encrypted_pdf" else "invalid_document"
        sys.stdout.write(json.dumps({"error": code}, separators=(",", ":")))
        return 0
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__" and "--worker" in sys.argv:
    raise SystemExit(_worker_main())
