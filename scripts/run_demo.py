"""Explicitly disposable loopback demo. Never reads .env or resets existing data.

Fixture mode creates a labeled non-AI incident with generated no-human evidence.
Inference mode runs the existing worker on one explicitly authorized local clip;
it never substitutes a fixture if the real model does not detect an event.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]
FIXTURE_LABEL = "Controlled Demo Fixture — Not Real AI Detection"


def prepare_demo(directory, mode, port, clip=None, manifest=None, rights_confirmed=False):
    """Initialize only a new empty caller-owned directory; no existing-data reset API."""
    from dormguard.config import Settings
    from dormguard.playback import digest_file, encode_clip
    from dormguard.security import hash_password
    from dormguard.store import Store

    directory = Path(directory).resolve()
    if mode not in {"fixture", "inference"} or not 1024 <= port <= 65535:
        raise ValueError("Select a supported demo mode and unprivileged port")
    if not directory.is_dir() or any(directory.iterdir()):
        raise ValueError("Demo requires a new empty private directory; existing data is never reset")
    if mode == "inference":
        if not clip or not Path(clip).is_file() or not manifest or not rights_confirmed:
            raise ValueError(
                "Inference requires an authorized local clip, pinned manifest and rights confirmation"
            )
        # Validate/load before publishing a workspace. Inference failure remains explicit.
        from dormguard.model import load_model

        candidate = Settings(directory, model_manifest=manifest, model_rights_confirmed=True)
        model = load_model(candidate)
        del model
    settings = Settings(
        directory,
        auth_mode="local",
        public_url=f"http://127.0.0.1:{port}",
        demo_mode=mode,
        model_manifest=manifest if mode == "inference" else None,
        model_rights_confirmed=rights_confirmed if mode == "inference" else False,
    )
    store = Store(directory)
    password = secrets.token_urlsafe(24)
    password_hash = hash_password(password)
    administrator = store.provision_user(
        "admin@demo.invalid", "super_admin", password_hash, display_name="Demo Administrator"
    )
    store.provision_user("director@demo.invalid", "director", password_hash, display_name="Demo Director")
    store.provision_user("deputy@demo.invalid", "deputy_director", password_hash, display_name="Demo Deputy")
    source_dir = directory / "test_sources"
    source_dir.mkdir(mode=0o700)
    source = source_dir / "demo.mp4"
    if mode == "fixture":
        import cv2
        import numpy as np

        frames = []
        for index in range(80):
            frame = np.full((240, 640, 3), (36, 31, 22), dtype=np.uint8)
            cv2.rectangle(frame, (20 + index * 5, 135), (75 + index * 5, 190), (60, 160, 160), -1)
            cv2.putText(
                frame, "CONTROLLED DEMO FIXTURE", (24, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (240, 240, 240), 2
            )
            cv2.putText(
                frame,
                "NOT REAL AI DETECTION - NO PEOPLE",
                (24, 85),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (240, 240, 240),
                1,
            )
            frames.append(frame)
        encode_clip(frames, 4, source)
    else:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(Path(clip).resolve()),
                "-t",
                "60",
                "-vf",
                "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "2",
                "-n",
                str(source),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=90,
        )
    camera = store.create_camera(
        FIXTURE_LABEL if mode == "fixture" else "Authorized prerecorded inference demo",
        "Disposable local demonstration",
        "test",
        "DORMGUARD_CAMERA_DEMO",
        administrator["id"],
    )
    incident = None
    if mode == "fixture":
        incident = store.create_incident(
            camera["id"],
            0.0,
            FIXTURE_LABEL,
            event_key="controlled-disposable-demo-fixture",
            evidence_source=source,
            clip_start=time.time() - 20,
            clip_end=time.time(),
        )
    access = {
        "origin": settings.public_url,
        "disposable": True,
        "mode": mode,
        "email": administrator["email"],
        "super_email": administrator["email"],
        "director_email": "director@demo.invalid",
        "deputy_email": "deputy@demo.invalid",
        "password": password,
        "camera_id": camera["id"],
        "incident_id": incident["id"] if incident else None,
        "source_sha256": digest_file(source),
        "label": FIXTURE_LABEL
        if mode == "fixture"
        else "Real pretrained inference; no guaranteed positive result",
    }
    access_file = directory / "demo-access.json"
    with open(access_file, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        json.dump(access, stream, indent=2)
        stream.write("\n")
    return settings, store, source, access_file


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-disposable", action="store_true")
    parser.add_argument("--mode", choices=("fixture", "inference"), required=True)
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--clip", type=Path)
    parser.add_argument("--authorized-footage", action="store_true")
    parser.add_argument("--model-manifest", type=Path)
    parser.add_argument("--model-rights-confirmed", action="store_true")
    args = parser.parse_args(argv)
    if not args.allow_disposable:
        parser.error("--allow-disposable is required; no data opened")
    if args.mode == "inference" and not (
        args.authorized_footage and args.clip and args.model_manifest and args.model_rights_confirmed
    ):
        parser.error(
            "Inference requires --clip, --authorized-footage, --model-manifest and --model-rights-confirmed"
        )
    if args.mode == "fixture" and (args.clip or args.model_manifest or args.model_rights_confirmed):
        parser.error("Fixture mode accepts neither footage nor model configuration")
    import uvicorn

    from dormguard.app import create_app

    with tempfile.TemporaryDirectory(prefix="dormguard-disposable-demo-") as directory:
        settings, store, source, access_file = prepare_demo(
            directory,
            args.mode,
            args.port,
            args.clip,
            args.model_manifest,
            args.model_rights_confirmed,
        )
        environment = {
            key: value for key, value in os.environ.items() if key in {"PATH", "TMPDIR", "LANG", "SYSTEMROOT"}
        }
        environment.update(
            DORMGUARD_DATA=directory,
            DORMGUARD_AUTH="local",
            DORMGUARD_ENV="evaluation",
            DORMGUARD_DEMO_MODE=args.mode,
            DORMGUARD_CAMERA_DEMO=str(source),
            DORMGUARD_PUBLIC_URL=settings.public_url,
        )
        if args.mode == "inference":
            environment.update(
                MODEL_MANIFEST=str(args.model_manifest.resolve()), DORMGUARD_MODEL_RIGHTS_CONFIRMED="1"
            )
        processes = []
        try:
            for module in ("dormguard.worker", "dormguard.notifications"):
                processes.append(subprocess.Popen([sys.executable, "-m", module], cwd=ROOT, env=environment))
            print(settings.public_url, flush=True)
            print("Private per-run sign-in details: " + str(access_file), flush=True)
            print(
                "Ctrl-C resets only this disposable workspace. Original footage and application data are untouched.",
                flush=True,
            )
            print(
                "External providers are disabled. Notification rows demonstrate workflow, not real delivery.",
                flush=True,
            )
            uvicorn.run(create_app(settings, store), host="127.0.0.1", port=args.port, access_log=False)
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
