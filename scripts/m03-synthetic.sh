#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
# No inherited application secrets and no dotenv file are permitted.
dotenv_parent="$repo_dir/src/main"
while :; do
  if [[ -e "$dotenv_parent/.env" ]]; then
    echo 'Synthetic checks refuse dotenv files in settings ancestors.' >&2
    exit 1
  fi
  [[ "$dotenv_parent" == / ]] && break
  dotenv_parent="$(dirname "$dotenv_parent")"
done
exec env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 \
  DJANGO_SETTINGS_MODULE=main.settings PYTHONPATH="$repo_dir/src" \
  SECRET_KEY=synthetic-m03-test-only PRODUCTION=0 \
  DATABASE_ENGINE=django.db.backends.sqlite3 \
  DATABASE_NAME=":memory:" \
  AWS_EC2_METADATA_DISABLED=true AWS_ACCESS_KEY_ID=synthetic \
  AWS_SECRET_ACCESS_KEY=synthetic \
  "$@"
