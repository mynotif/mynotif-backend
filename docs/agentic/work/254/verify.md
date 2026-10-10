# Verify — #254

Status: PASS locally. Python 3.13.1, uv 0.11.12. New checkout `make virtualenv/test`: PASS. The dev lock includes the editable `mynotif==0.1` package and 79 pinned dependencies; `importlib.metadata.version("mynotif")` returns `0.1`. `make lint`: PASS both before and after pytest (isort, Flake8, Ruff, Terraform and Prettier); the latter required ignoring `src/.pytest_cache/` in the root `.gitignore`. `make unittest`: 221 passed, 9 existing warnings, 99% coverage. `make run/migrations/check`: PASS, no changes detected. `make lock/check`: PASS; 55/55 production pins occur unchanged in the dev lock. `git diff --check`: PASS.

Docker image built successfully with pip from unchanged `requirements.txt`; container smoke import reports Django 5.1.4 and DRF 3.15.2. Lambda lock and packaging files are unchanged. No deployment or external data access.

Local installation timings, one run each on Linux with Python 3.13.1 and the same application, editable package and dev dependencies: fresh venv plus empty cache: pip 27.14 s, uv 3.13 s; repeat on the same venv and warm cache: pip 2.61 s, uv 0.06 s. Network and runner conditions can affect these samples. Baseline #255 GitHub Tests checks completed in 1m30s and 1m16s; #254 CI comparison awaits the PR. No CI speedup is claimed yet.
