#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
venv_dir="${STOCK_VALIDATOR_VENV:-$repo_dir/.venv}"
python3.12 -m venv "$venv_dir"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$repo_dir/.cache/pip}"
"$venv_dir/bin/python" -m pip install --disable-pip-version-check -r requirements-dev.txt
"$venv_dir/bin/python" -m pip install --disable-pip-version-check --no-build-isolation -e .
"$venv_dir/bin/python" -m stock_validator certify --output reports/engine-certificate.json
