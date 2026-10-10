# Design — #255

Status: PASS. Pin Ruff 0.14.1 in the dev extra and configure Python 3.13, line length 88 and Black compatible formatting in `pyproject.toml`. Replace Black commands with `ruff format` and `ruff format --check` in Makefile and CI. Keep isort and Flake8 active: their existing checks remain the authoritative lint gate. Ruff E4/E7/E9/F and I are candidate equivalents, but this ticket does not replace them without a separately reviewed rule migration. Reformat the 10 affected Python files mechanically. No API, data, auth or deployment change.
