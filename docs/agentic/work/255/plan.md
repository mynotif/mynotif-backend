# Plan — #255

Status: PASS. Worktree `255-ruff`, branch `chore/255-ruff`, one ticket/commit/PR. Edit `Makefile`, `setup.cfg`, `.github/workflows/tests.yml`, `README.md`, `pyproject.toml` and only Python files reported by `ruff format --check`. Compare rule coverage; run installation, `make lint`, `make unittest`, migration check, and time Black/Ruff on the same pre-change tree with warm tool caches. Independently review the diff, then open PR, await current CI and Lead Tech review before merge. No schema migration required. Keep Docker and Lambda packaging unchanged.
