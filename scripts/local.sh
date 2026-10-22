#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python_bin="${DORMGUARD_PYTHON:-.venv/bin/python}"
if [ ! -x "$python_bin" ]; then
  echo "Create .venv with Python 3.12 and install requirements-dev.txt first." >&2
  exit 2
fi
# No implicit .env loading: especially never import real provider settings into tests.
case "${1:-}" in
  serve) shift; exec "$python_bin" -m dormguard.app "$@" ;;
  worker) shift; exec "$python_bin" -m dormguard.worker "$@" ;;
  notifications) shift; exec "$python_bin" -m dormguard.notifications "$@" ;;
  test) shift; exec "$python_bin" -m unittest discover -s tests -v "$@" ;;
  *) echo "Usage: scripts/local.sh serve|worker|notifications|test" >&2; exit 2 ;;
esac
