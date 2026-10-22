"""Fetch one pinned, publisher Apache-2.0 checkpoint for permitted evaluation.

No dynamic revisions, executable pickle weights, model code or training. Each
download must match an independently recorded digest. The manifest is suitable
for generated-fixture evaluation; operational deployment needs separate review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = "https://huggingface.co/jaranohaal/vit-base-violence-detection/resolve/31931091dfd4ea08a30c42be0db8e1488263cbd5/"
NOTEBOOK = "https://raw.githubusercontent.com/jaranohaal/vit-base-violence-detection/0961a54da65dffffd95ac3abda869e4cbc4a674a/ViT-base-Violence-Detection.ipynb"
FILES = {
    "model.safetensors": (
        MODEL_ROOT + "model.safetensors",
        "9eec3237288742eb4267cb2cbbbdacba5bee889be4673215bfe40e1dbd5621a5",
    ),
    "config.json": (
        MODEL_ROOT + "config.json",
        "03652439b83bb9626e74675127b66e0278da16d74660e581a3be48663f75c626",
    ),
    "preprocessor_config.json": (
        MODEL_ROOT + "preprocessor_config.json",
        "5e6895a8a80efa00a98656b6c24d202d7582614ceea87aa122f861120dc833c0",
    ),
    "README.md": (
        MODEL_ROOT + "README.md",
        "eec62e115b63cf716cdd6c6cda33580b6afe476f5e24a55185e754a832f87076",
    ),
    "SOURCE_NOTEBOOK.ipynb": (NOTEBOOK, "a1a229fac9961a1797752139e3a02eb826ab3b140ee0d8376e92bc8a7a0aa7ec"),
}


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def fetch(path, url, expected):
    if path.is_symlink():
        raise ValueError("checkpoint_symlink_refused")
    if path.exists():
        if digest(path) != expected:
            raise ValueError("existing_file_digest_mismatch_refusing_overwrite")
        return "verified_existing"
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
    try:
        try:
            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            context = ssl.create_default_context()
        with (
            urllib.request.urlopen(url, timeout=30, context=context) as response,
            temporary.open("xb") as stream,
        ):
            if not response.url.startswith("https://"):
                raise ValueError("insecure_download_redirect")
            os.chmod(temporary, 0o600)
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 400 * 1024 * 1024:
                    raise ValueError("checkpoint_size_limit")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if digest(temporary) != expected:
            raise ValueError("download_digest_mismatch")
        # Hard-link gives exclusive creation even if a concurrent fetch finishes.
        os.link(temporary, path)
        return "downloaded_verified"
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rights-confirmed", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "checkpoints" / "jaranohaal-vit")
    args = parser.parse_args(argv)
    if not args.rights_confirmed:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "confirm_permitted_evaluation_scope_after_reading_MODEL_RESEARCH",
                }
            )
        )
        return 2
    try:
        destination = args.output.expanduser().resolve()
        if args.output.is_symlink():
            raise ValueError("checkpoint_directory_symlink_refused")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        states = {name: fetch(destination / name, url, expected) for name, (url, expected) in FILES.items()}
        manifest = json.loads((ROOT / "docs/engineering/MODEL_MANIFEST_EXAMPLE.json").read_text())
        manifest["checkpoint_dir"] = "."
        target = destination / "manifest.json"
        desired = json.dumps(manifest, indent=2) + "\n"
        if target.exists():
            if target.is_symlink() or target.read_text() != desired:
                raise ValueError("existing_manifest_differs_refusing_overwrite")
        else:
            with target.open("x") as stream:
                stream.write(desired)
            os.chmod(target, 0o600)
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "scope": "pinned public artifacts, digest verification only; inference NOT_RUN",
                    "revision": manifest["revision"],
                    "files": states,
                },
                indent=2,
            )
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "reason": str(error) if isinstance(error, ValueError) else type(error).__name__,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
