# Research — #255

Status: PASS. Entry: EXISTING, STANDARD. The issue specifies scope and acceptance criteria; product phases and UI design system are outside this tooling change. No approved architecture decision is changed.

Observed stack: Python 3.13, Django and DRF (`setup.cfg`), pip based development (`Makefile`), production pins in `requirements.txt`, CI in `.github/workflows/tests.yml`. Black 24.10.0 is installed explicitly in CI; isort uses its Black profile and Flake8 uses line length 88 with E203 ignored. Terraform and Prettier are separate checks. Black accepted all 74 Python files before the change. Ruff 0.14.1 would reformat 10 of them.

Auth, authorization, tenant isolation, health data, secrets and external actions: application behavior is untouched. CI continues to use isolated test credentials; formatter only reads local code. No schema or API contract changes. Risk: formatter differences, lint coverage loss, or accidental source edits. Verify mechanical diffs, full lint, tests and migration check. Rollback: revert this single commit. Dependency: #254 also edits Makefile, dev dependencies, CI and README; merge this ticket first and base #254 on the result.
