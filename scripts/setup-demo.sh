#!/bin/sh
# Explicit online setup only. Launch/check never install or download anything.
set -eu
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  bootstrap="${DORMGUARD_BOOTSTRAP_PYTHON:-python3.12}"
  "$bootstrap" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 is required"'
  "$bootstrap" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements-local.txt
.venv/bin/python - <<'PY'
import os
from pathlib import Path
import imageio_ffmpeg

target = Path('.venv/bin/ffmpeg')
if not target.exists() and not target.is_symlink():
    target.symlink_to(imageio_ffmpeg.get_ffmpeg_exe())
    os.chmod(target, 0o755)
print('Runtime installed. Model weights and permitted media must already be provisioned separately.')
print('Next: ./scripts/check-offline-demo.sh --model-rights-confirmed')
PY
