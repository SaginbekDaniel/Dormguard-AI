"""Prepare a NEW private persistent volume locally; no provider calls or public assets.

The generated credential is saved with mode 0600, never printed. Copy the resulting
directory privately to the service's /app/data volume before first launch.
"""

import argparse
import json
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def prepare(directory, origin, manifest, assets, rights_confirmed):
    from dormguard.config import Settings
    from dormguard.model import load_model
    from dormguard.offline import write_private
    from dormguard.security import hash_password
    from dormguard.store import Store
    from scripts.offline_demo import prepare_sources, read_assets, sha256

    if not rights_confirmed:
        raise ValueError("Explicit permitted noncommercial model and test-media scope is required")
    directory, manifest, assets = Path(directory), Path(manifest).resolve(), Path(assets).resolve()
    if directory.is_symlink() or directory.exists():
        raise ValueError("Choose a new private directory; existing data is never overwritten")
    # Validate origin/model/media before creating any users or persistent application data.
    settings = Settings(
        directory,
        auth_mode="local",
        runtime_mode="online_demo",
        https=True,
        public_url=origin,
        model_manifest=manifest,
        model_rights_confirmed=True,
    )
    adapter = load_model(settings)
    model_name = adapter.name
    del adapter
    sources = read_assets(assets)
    positive = next((source for source in sources if source["scenario"] == "staged_physical_conflict"), None)
    if not positive:
        raise ValueError("A permitted staged physical-conflict source is required")
    data = json.loads(manifest.read_text())
    if data.get("format") != "videomae_safetensors_demo":
        raise ValueError("This provisioner accepts only the reviewed noncommercial demo VideoMAE model")
    directory.mkdir(mode=0o700, parents=True)
    model_dir = directory / "model"
    model_dir.mkdir(mode=0o700)
    checkpoint = (manifest.parent / data["checkpoint_dir"]).resolve()
    for name, expected in data["files"].items():
        target = model_dir / name
        shutil.copyfile(checkpoint / name, target)
        if sha256(target) != expected:
            raise ValueError("Copied model digest mismatch")
        target.chmod(0o400)
    data["checkpoint_dir"] = "."
    write_private(model_dir / "manifest.json", data)
    prepared = prepare_sources(directory, [positive])[0]
    source_dir = directory / "test_sources"
    source_dir.mkdir(mode=0o700)
    source = source_dir / "permitted-demo.mp4"
    shutil.copyfile(prepared["prepared_file"], source)
    source.chmod(0o400)
    # Keep provenance adjacent to the prepared source, outside all public static paths.
    write_private(source_dir / "provenance.json", prepared)
    password = secrets.token_urlsafe(32)
    store = Store(directory)
    try:
        director = store.provision_user(
            "director@online.demo.invalid",
            "director",
            hash_password(password),
            display_name="Demo Dormitory Director",
        )
        camera = store.create_camera(
            positive["name"],
            "Prerecorded noncommercial demonstration",
            "test",
            "DORMGUARD_CAMERA_ONLINE",
            director["id"],
        )
        store.update_camera(camera["id"], {"enabled": False}, director["id"])
    finally:
        store.db.close()
    write_private(
        directory / "online-access.json",
        {
            "origin": origin,
            "email": director["email"],
            "password": password,
            "camera_id": camera["id"],
            "scope": "Private noncommercial demonstration only",
        },
    )
    return {
        "status": "PASS",
        "model": model_name,
        "credential_file": str(directory / "online-access.json"),
        "persistent_volume": str(directory),
        "camera_enabled": False,
        "public_deployment": "NOT_RUN",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument(
        "--model-manifest", type=Path, default=ROOT / "docs/ai/OFFLINE_VIDEOMAE_MANIFEST.json"
    )
    parser.add_argument("--assets", type=Path, default=ROOT / "docs/ai/OFFLINE_DEMO_ASSETS.json")
    parser.add_argument("--noncommercial-rights-confirmed", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(
                args.data, args.origin, args.model_manifest, args.assets, args.noncommercial_rights_confirmed
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
