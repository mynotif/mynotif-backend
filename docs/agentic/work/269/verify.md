# Verify — #269

VERIFY: PASS for implementation, pending committed CI and final Lead Tech.
Tested working tree: branch `feat/269-m03-prescriptions`, parent
`641df283e8d4edec7ab00a06d5f0f5f44ceda75e`, with the single #269 change.
Normative mobile contract blob: `821310cbe0da90aa58a181a6c98cf2458ba13e3f`.
Final committed SHA and CI evidence will be recorded in the PR.

## Commands actually executed

```sh
scripts/m03-synthetic.sh make test
scripts/m03-synthetic.sh make run/migrations/check
scripts/m03-synthetic.sh venv/bin/python scripts/m03-http-proof.py
scripts/m03-synthetic.sh env DATABASE_ENGINE=django.db.backends.postgresql DATABASE_NAME=m03_269 DATABASE_HOST=127.0.0.1 DATABASE_PORT=32769 DATABASE_USER=m03_synthetic DATABASE_PASSWORD=m03_synthetic venv/bin/pytest -q --tb=short src/tests/nurse/test_read_patients_v2.py src/tests/nurse/test_read_prescriptions_v2.py src/tests/nurse/test_read_privacy.py src/tests/main/test_session_api.py src/tests/main/test_session_services.py src/tests/main/test_session_concurrency.py
git diff --check
```

PostgreSQL 16 focused reads and M02 sessions/concurrency: **167 passed**,
2 existing warnings, 209.26s. Migration check: **No changes detected**.
Actual migration history was used; no temporary schema plugin.
Complete SQLite suite: **438 passed, 6 skipped**, 317.80s, coverage 95%
(includes tests). Five skips require PostgreSQL concurrency; one is PostgreSQL
RLS. isort, flake8, Ruff, Terraform formatting and Prettier PASS.

## Actual availability and coverage

Real loopback Gunicorn exercised all three registered paths: authenticated
GET/HEAD/OPTIONS 200 and anonymous GET 401 with exact JSON and Bearer challenge.
HEAD has no body; private/no-store and Pragma no-cache verified. Patient page 2
returns 5 of 25 fictional patients. Search marker, token and raw nested paths
absent from actual Gunicorn logs. Explicit packaged Gunicorn config loaded.
This proves local route availability, not hosted HTTPS or proxy behavior.

Tests cover auth absent/invalid/expired/revoked/historical Token, Nurse absence
without creation, staff A/B and explicit shared patient rights, foreign patient
and prescription IDs including another authorized patient's prescription,
malformed/empty/overflow IDs, rights before invalid query, exact five fields and
source dates, no document/URL/email/clinical status/storage/S3 call, stable
end_date/id descending order, 45/101-row pagination, empty/end/out-of-range pages,
unknown/duplicated/invalid queries including rejected search, HEAD/OPTIONS/405,
cache and closed OpenAPI schemas. List uses four queries, detail three, with only
summary columns selected. Common patient search/privacy tests and v1/M02
regressions are included in the complete suite.

Independent reviewer replay: **63 passed**, 2 existing warnings, 91.91s.
See review.md; final committed CI and separate Lead Tech remain PR gates.

## Environment and limits

Python 3.13.1, Django 5.1.4, DRF 3.15.2, locked dependencies. Runner rejects
ancestor .env files and clears inherited configuration. SQLite disposable;
PostgreSQL uses a local synthetic temporary container. No real data, provider
spending, cloud operation, production migration or deployment.

Foundation #267 records existing broader PostgreSQL regression failures,
reproduced on baseline; the focused suites here pass, not a claim that all
historical PostgreSQL tests pass. FORCE RLS without policies hides associations
from ordinary roles: operational database role must be qualified before
activation. Retire old writers and repair/check normalized columns before
activating search. Upstream proxy/App Runner/database logs remain unqualified
before real data. No production performance/SLA or hosted availability claimed.
Backend 401 responses do not validate mobile refresh/purge/navigation.

Delivery stops at READY_FOR_HUMAN after CI and Lead Tech, without merge or
deployment. Mobile #70/#71 and parent #10 remain open.
