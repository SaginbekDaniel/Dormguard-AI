"""Disposable real API/browser test server. NEVER a production bootstrap.

Uses generated no-human video and an explicitly controlled incident fixture.
Local password authentication is real; external providers are unconfigured.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import cv2
    import numpy as np
    import uvicorn

    from dormguard.app import create_app
    from dormguard.config import Settings
    from dormguard.playback import encode_clip
    from dormguard.security import hash_password
    from dormguard.store import Store

    with tempfile.TemporaryDirectory(prefix="dormguard-browser-fixture-") as directory:
        settings = Settings(Path(directory), auth_mode="local", public_url="http://127.0.0.1:8765")
        store = Store(settings.data_dir)
        password = "controlled-browser-test-password"
        access = settings.data_dir / "browser-access.json"
        access.write_text(
            json.dumps(
                {
                    "origin": settings.public_url,
                    "email": "director@example.invalid",
                    "password": password,
                    "super_email": "super@example.invalid",
                    "disposable": True,
                }
            )
        )
        access.chmod(0o600)
        director = store.provision_user("director@example.invalid", "director", hash_password(password))
        store.provision_user("deputy@example.invalid", "deputy_director", hash_password(password))
        store.provision_user(
            "super@example.invalid",
            "super_admin",
            hash_password(password),
            display_name="Controlled Administrator",
        )
        source_dir = settings.data_dir / "test_sources"
        source_dir.mkdir(mode=0o700)
        source = source_dir / "generated.mp4"
        frames = []
        for index in range(40):
            frame = np.zeros((180, 320, 3), dtype=np.uint8)
            frame[:] = (40, 31, 21)
            cv2.rectangle(frame, (20 + index * 4, 75), (65 + index * 4, 120), (40, 160, 160), -1)
            cv2.putText(
                frame, "GENERATED TEST SOURCE", (12, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (230, 230, 230), 1
            )
            frames.append(frame)
        encode_clip(frames, 4, source)
        camera = store.create_camera(
            "Transport test camera",
            "Controlled test workspace",
            "test",
            "DORMGUARD_CAMERA_TEST",
            director["id"],
        )
        incident = store.create_incident(
            camera["id"],
            0.81,
            "CONTROLLED_BROWSER_FIXTURE_NOT_MODEL_INFERENCE",
            event_key="controlled-browser-fixture",
            evidence_source=source,
            clip_start=time.time() - 10,
            clip_end=time.time(),
        )
        fixture_access = json.loads(access.read_text())
        fixture_access["incident_id"] = incident["id"]
        access.write_text(json.dumps(fixture_access))
        # A fresh explicit environment prevents accidentally loading real provider/DB credentials.
        environment = {
            key: value for key, value in os.environ.items() if key in {"PATH", "TMPDIR", "LANG", "SYSTEMROOT"}
        }
        environment.update(
            DORMGUARD_DATA=directory, DORMGUARD_AUTH="local", DORMGUARD_CAMERA_TEST=str(source)
        )
        worker = subprocess.Popen(
            [sys.executable, "-m", "dormguard.worker"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
        )
        try:
            print("DISPOSABLE TEST SERVER http://127.0.0.1:8765", flush=True)
            print("Browser test access: set DORMGUARD_BROWSER_ACCESS=" + str(access), flush=True)
            print(
                "Credentials are the controlled fixture constants in tests/serve_browser_fixture.py, not default application accounts.",
                flush=True,
            )
            uvicorn.run(create_app(settings, store), host="127.0.0.1", port=8765, access_log=False)
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)


if __name__ == "__main__":
    main()
