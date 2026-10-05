#!/usr/bin/env bash
# Read-only; no bootstrap or lifecycle mutations.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'BLOCKED Python: python3 missing' 'RESULT BLOCKED'
  exit 1
fi
exec python3 "$script_dir/doctor.py" "$@"
