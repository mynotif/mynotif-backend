# OrdoPro backend

[![Tests](https://github.com/mynotif/mynotif-backend/actions/workflows/tests.yml/badge.svg)](https://github.com/mynotif/mynotif-backend/actions/workflows/tests.yml)
[![codecov](https://codecov.io/github/mynotif/mynotif-backend/graph/badge.svg?token=RPS12B9119)](https://codecov.io/github/mynotif/mynotif-backend)
[![Deployment](https://github.com/mynotif/mynotif-backend/actions/workflows/deployment.yml/badge.svg)](https://github.com/mynotif/mynotif-backend/actions/workflows/deployment.yml)

## URLs

- :tada: https://api.ordopro.fr/
- :memo: https://api.ordopro.fr/swagger/
- :memo: https://api.ordopro.fr/redoc/
- :goal_net: https://andre-5t.sentry.io/issues/?project=6257099

## Install

Install Python 3.13 and uv 0.11.12 first. `make` checks the uv version. From a
clean checkout, the development environment is synchronized from the committed
`requirements-dev.txt` lock, including the editable local package and production
pins in `requirements.txt`:

```sh
make virtualenv/test
cp .env.example .env
```

## :tada: run

```sh
make run/dev
```

## :test_tube: test

```sh
make unittest
```

## Continuous integration and deployment

Pushes and pull requests run the `Tests` workflow with read-only repository
permissions. It installs dependencies, runs lint and the synthetic pytest suite,
and checks for missing migrations against disposable in-memory SQLite. It does
not need AWS credentials or a remote database. External services are mocked in
the tests; Sentry and instance-metadata credential lookup are disabled. CI rejects
`.env` files in the checkout and its ancestor directories before loading settings.
Dependency and tooling downloads still require network access. CI pins uv 0.11.12
and caches uv downloads by the requirements files. The dev lock pins Ruff and
the other Python test tools. It is generated for Python 3.13.

Coverage is generated locally as `coverage.xml` and HTML by `make unittest`; CI
no longer uploads it to Codecov or uses its token. The badge above is historical.
SQLite tests do not establish PostgreSQL or production compatibility. To reproduce
the checks, use a clean checkout without a `.env`, Python 3.13, uv 0.11.12,
Terraform and Node:

```sh
make virtualenv/test
make lint
DATABASE_ENGINE=django.db.backends.sqlite3 DATABASE_NAME=:memory: \
  DATABASE_HOST= DATABASE_USER= DATABASE_PASSWORD= DATABASE_PORT= \
  PRODUCTION=0 SENTRY_DSN= AWS_EC2_METADATA_DISABLED=true make unittest
DATABASE_ENGINE=django.db.backends.sqlite3 DATABASE_NAME=:memory: \
  DATABASE_HOST= DATABASE_USER= DATABASE_PASSWORD= DATABASE_PORT= \
  PRODUCTION=0 SENTRY_DSN= AWS_EC2_METADATA_DISABLED=true make run/migrations/check
```

Use an environment without real provider credentials or inherited application
configuration. The workflow contains the full synthetic environment. Do not copy
production configuration to run these checks.

`Deployment` is disabled in repository Actions settings and must remain disabled
after this change. Its only code trigger is manual `workflow_dispatch`: every
sensitive job also requires `main` and the `confirm_production` input (false by
default). A passing CI or merged PR does not authorize deployment. Re-enabling
and dispatching it require a separate production operation with authorization,
current CI, backup/migration review and a rollback plan. No production dispatch
is needed to validate this change. The gated migrations job provisions the same
pinned uv version before invoking the Makefile; Docker keeps its pip based image.

The first push of a branch changing these workflows must already contain the
corrected deployment trigger; never publish that branch at the old revision first.
Before publishing, confirm Deployment is disabled and inspect active runs without
blindly cancelling them. If reverting the CI change, keep Deployment disabled and
retain its manual-only gates; do not restore automatic deployment on push.

## :rotating_light: linting

```sh
make lint
make format
```

Python formatting uses Ruff 0.14.1 (`make lint/ruff` checks without writing;
`make format/ruff` applies formatting). isort and Flake8 remain separate lint
checks with their existing rules. Terraform and Prettier checks are unchanged.

## Requirements bump

The production lock is `requirements.txt`; `requirements-dev.txt` adds pinned
development tools while constraining all shared packages to production versions.
CI runs `make lock/check` to catch stale dev pins. Review lock diffs before
committing them. Regenerate only the intended lock:

```sh
make -B requirements.txt
make -B requirements-dev.txt
```

These commands use `uv pip compile`; neither requests a dependency upgrade by
default. Docker continues to install the production lock with pip, without uv
in the runtime image. Lambda packaging continues to use its existing Python
3.12 `src/lambdas/requirements.txt` lock.

## Docker

```sh
make docker/build
make docker/run
```

## Terraform

Deployment:

```sh
AWS_PROFILE=<aws_profile> make devops/terraform/plan
```

## Learn More

You can learn more in the api rest framework(https://www.django-rest-framework.org/)

You can learn more build API(https://insomnia.rest/)

You can learn more in django(https://www.djangoproject.com/)
