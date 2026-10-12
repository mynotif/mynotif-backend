# Verify — #268

VERIFY: PASS for implementation, pending committed CI and final Lead Tech.

Normative mobile contract blob: `821310cbe0da90aa58a181a6c98cf2458ba13e3f`.
Tested working tree: branch `feat/268-m03-patients`, parent
`de1bda39f0e3bd39fc9ef01eab1870c9d4fb42fe`, with the single #268 change.
The final committed SHA and CI evidence will be recorded in the PR.

## Commands actually executed

```sh
scripts/m03-synthetic.sh make test
scripts/m03-synthetic.sh make run/migrations/check
scripts/m03-synthetic.sh venv/bin/python scripts/m03-http-proof.py
scripts/m03-synthetic.sh env DATABASE_ENGINE=django.db.backends.postgresql DATABASE_NAME=m03_268 DATABASE_HOST=127.0.0.1 DATABASE_PORT=32769 DATABASE_USER=m03_synthetic DATABASE_PASSWORD=m03_synthetic venv/bin/pytest -q --tb=short src/tests/nurse/test_read_patients_v2.py src/tests/nurse/test_read_privacy.py src/tests/nurse/test_patient_search.py src/tests/nurse/test_patient_search_migrations.py src/tests/nurse/test_patient_search_postgres.py src/tests/main/test_session_api.py src/tests/main/test_session_services.py
git diff --check
```

Complete SQLite suite: **375 passed, 6 skipped**, 210.24s, coverage 95%
(includes tests). Five skips require PostgreSQL session concurrency; one is
PostgreSQL RLS. isort, flake8, Ruff, Terraform formatting and Prettier PASS.
Migration check: **No changes detected**. Actual migration history was used;
no temporary schema plugin. PostgreSQL 16 focused suite: **130 passed,
1 skipped**, 94.09s; the skip is SQLite-only NUL search.

Actual loopback Gunicorn HTTP: registered patient collection GET/HEAD/OPTIONS
200, anonymous GET 401, page 2 returns 5 of 25 fictional patients; global
search marker absent from logs, along with tokens and raw nested paths.
The script explicitly loads the packaged Gunicorn configuration. This proves
local route availability, not hosted HTTPS or proxy behavior.

## Coverage and environment

Strict Bearer M02 states, Token refusal, no Nurse creation, A/B and staff
isolation, six exact patient fields, closed pagination envelope, count and
stable ordering, strict duplicated/unknown/invalid parameters, Unicode global
SQL search before LIMIT/OFFSET, exact controlled errors, HEAD/OPTIONS/CORS,
private/no-store, exception and structured log redaction, OpenAPI, v1 and M02
regressions pass. Nonempty patient page uses three database queries.

Python 3.13.1, Django 5.1.4, DRF 3.15.2, locked dependencies. The runner rejects
ancestor .env files and clears inherited configuration. SQLite is disposable;
PostgreSQL uses a local temporary synthetic container. No real data, cloud
operation, production migration or deployment was used.

## Limits

Foundation #267 records existing full-PostgreSQL regression failures (hardcoded
primary keys and shared throttle cache), reproduced on baseline; this focused
suite passes, not a claim that all existing PostgreSQL tests pass. Existing
FORCE RLS without policies hides associations from ordinary PostgreSQL roles;
target-role qualification remains required before activation. Retire old writers
and repair/check normalized shadows before activating search. Upstream proxy,
App Runner and database logging remain unqualified before real data. Unexpected
500 errors keep their existing generic JSON status; they are not misreported as
operational 503. No mobile renewal, purge or navigation is validated.

## Reviews

Independent precommit technical review recorded in review.md. Final committed
CI and separate Lead Tech attestation belong to the PR, avoiding another commit
solely to embed that commit's own SHA. Delivery stops at READY_FOR_HUMAN, with
no merge or deployment; mobile #70/#71 and parent #10 remain open.
