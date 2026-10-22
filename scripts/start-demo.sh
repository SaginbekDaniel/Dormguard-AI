#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
PATH="$PWD/.venv/bin:$PATH"
export PATH
python_bin="${DORMGUARD_PYTHON:-.venv/bin/python}"
if [ ! -x "$python_bin" ]; then echo 'Missing local Python environment. Read docs/OFFLINE_SETUP.md.' >&2; exit 2; fi
exec "$python_bin" scripts/offline_demo.py start "$@"
