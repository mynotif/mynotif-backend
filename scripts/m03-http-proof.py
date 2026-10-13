"""Exercise registered routes through synthetic loopback HTTP, never real data.

Run with scripts/m03-synthetic.sh venv/bin/python scripts/m03-http-proof.py.
This creates its own disposable SQLite database and one Gunicorn worker.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

if os.environ.get("SECRET_KEY") != "synthetic-m03-test-only":
    raise SystemExit("Use the isolated synthetic runner.")
if os.environ.get("PRODUCTION") != "0":
    raise SystemExit("Only a synthetic local environment is permitted.")

ROOT = Path(__file__).resolve().parents[1]
MARKER = "SYNTHETIC_M03_SEARCH_7349"


def request(port, path, bearer=None, method="GET"):
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers, method=method
    )
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read()
        return response.status, response.headers, json.loads(body) if body else None


with tempfile.TemporaryDirectory(prefix="m03-http-proof-") as temporary:
    os.environ["DATABASE_ENGINE"] = "django.db.backends.sqlite3"
    os.environ["DATABASE_NAME"] = str(Path(temporary) / "synthetic.sqlite3")
    os.environ["DJANGO_SETTINGS_MODULE"] = "main.settings"
    sys.path.insert(0, str(ROOT / "src"))
    import django

    django.setup()
    from django.contrib.auth.models import User
    from django.core.management import call_command

    from main.session_services import login
    from nurse.models import Nurse, Patient, Prescription
    from nurse.read_privacy import read_route

    call_command("migrate", verbosity=0, interactive=False)
    user = User.objects.create_user(
        username="m03-http@example.invalid", password="Synthetic-HTTP-Only!397"
    )
    nurse = Nurse.objects.create(user=user)
    rows = Patient.objects.bulk_create(
        [
            Patient(firstname="Fictif", lastname="Exemple", city="Lyon")
            for _ in range(25)
        ]
    )
    nurse.patients.add(*rows)
    rx = Prescription.objects.create(
        patient=rows[-1],
        prescribing_doctor="Fictif",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        photo_prescription="synthetic-unused.png",
    )
    bearer = login(user.username, "Synthetic-HTTP-Only!397")["access_token"]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log_path = Path(temporary) / "server.log"
    with log_path.open("w+") as output:
        server = subprocess.Popen(
            [
                str(ROOT / "venv/bin/gunicorn"),
                "--config",
                str(ROOT / "gunicorn.conf.py"),
                "--chdir",
                str(ROOT / "src"),
                "--workers",
                "1",
                "--bind",
                f"127.0.0.1:{port}",
                "--access-logfile",
                "-",
                "main.wsgi",
            ],
            stdout=output,
            stderr=output,
            env=os.environ.copy(),
        )
        evidence = []
        try:
            for _ in range(100):
                try:
                    request(port, "/api/v1/version/")
                    break
                except (OSError, urllib.error.URLError):
                    if server.poll() is not None:
                        raise RuntimeError(
                            "Synthetic server failed to start."
                        ) from None
                    time.sleep(0.1)
            else:
                raise RuntimeError("Synthetic server startup timed out.")
            paths = ["/api/v2/patients/"]
            # The same proof script covers the two later #269 routes when registered.
            from django.urls import Resolver404, resolve

            rx_list = f"/api/v2/patients/{rows[-1].pk}/prescriptions/"
            rx_detail = rx_list + f"{rx.pk}/"
            try:
                resolve(rx_detail)
            except Resolver404:
                pass
            else:
                paths.extend([rx_list, rx_detail])
            for path in paths:
                for method in ("GET", "HEAD", "OPTIONS"):
                    status, headers, body = request(port, path, bearer, method)
                    assert status == 200
                    assert headers["Cache-Control"] == "private, no-store"
                    assert headers["Pragma"] == "no-cache"
                    if method == "HEAD":
                        assert body is None
                    if method == "OPTIONS":
                        assert body == {"allowed_methods": ["GET", "HEAD", "OPTIONS"]}
                    evidence.append(
                        {"route": read_route(path), "method": method, "status": status}
                    )
                status, headers, body = request(port, path + "?search=" + MARKER)
                assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
                assert body == {
                    "code": "authentication_required",
                    "message": "Authentification requise.",
                }
                evidence.append(
                    {"route": read_route(path), "method": "GET", "status": status}
                )
            status, _, page = request(port, "/api/v2/patients/?page=2", bearer)
            assert status == 200 and page["count"] == 25 and len(page["results"]) == 5
            status, _, page = request(
                port, "/api/v2/patients/?search=" + MARKER, bearer
            )
            assert status == 200 and page["count"] == 0
        finally:
            server.terminate()
            server.wait(timeout=15)
        output.flush()
    logs = log_path.read_text()
    assert MARKER not in logs and bearer not in logs
    assert rx_list not in logs and rx_detail not in logs
    print(
        json.dumps(
            {
                "transport": "synthetic loopback HTTP",
                "checks": evidence,
                "pagination": "PASS",
                "log_marker": "ABSENT",
            },
            ensure_ascii=False,
        )
    )
