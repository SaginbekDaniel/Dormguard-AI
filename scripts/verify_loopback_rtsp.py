"""Disposable loopback RTSP publish/read/disconnect/reconnect; no physical camera.

Starts one task-owned MediaMTX container on a random loopback port, publishes a
generated no-human fixture, exercises the actual camera worker, then cleans only
its own temporary processes/container. Requires explicit --allow-disposable.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.PIPE, timeout=60).strip()


def wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def terminate(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run():
    import cv2

    from dormguard.config import Settings
    from dormguard.store import Store
    from dormguard.worker import Worker
    from scripts.verify_rtsp import generate_fixture

    name = "dormguard-rtsp-verify-" + uuid.uuid4().hex[:8]
    publisher, thread, worker = None, None, None
    created = False
    started = time.monotonic()
    try:
        docker(
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--publish",
            "127.0.0.1::8554",
            "--env",
            "MTX_RTSPTRANSPORTS=tcp",
            "bluenviron/mediamtx:1.21.2",
        )
        created = True
        mapping = docker("port", name, "8554/tcp")
        if not mapping.startswith("127.0.0.1:"):
            raise RuntimeError("loopback_mapping_required")
        port = int(mapping.rsplit(":", 1)[1])
        source_url = f"rtsp://127.0.0.1:{port}/generated"
        with tempfile.TemporaryDirectory(prefix="dormguard-rtsp-") as temporary:
            settings = Settings(Path(temporary), auth_mode="local", camera_networks="127.0.0.1/32")
            store = Store(settings.data_dir)
            director = store.provision_user("director@example.invalid", "director")
            camera = store.create_camera(
                "Generated loopback RTSP",
                "No physical camera",
                "rtsp",
                "DORMGUARD_CAMERA_LOOPBACK",
                director["id"],
            )
            source = generate_fixture(settings.data_dir / "generated.avi")

            def publish():
                return subprocess.Popen(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-re",
                        "-stream_loop",
                        "-1",
                        "-i",
                        str(source),
                        "-t",
                        "55",
                        "-an",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-tune",
                        "zerolatency",
                        "-pix_fmt",
                        "yuv420p",
                        "-g",
                        "10",
                        "-keyint_min",
                        "10",
                        "-threads",
                        "2",
                        "-rtsp_transport",
                        "tcp",
                        "-f",
                        "rtsp",
                        source_url,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            publisher = publish()
            worker = Worker(settings, store)
            errors = []

            def exercise():
                try:
                    worker.run()
                except Exception as error:
                    errors.append(type(error).__name__)

            with patch.dict(os.environ, {"DORMGUARD_CAMERA_LOOPBACK": source_url}):
                thread = threading.Thread(target=exercise)
                thread.start()
                preview = settings.data_dir / "frames" / (camera["id"] + ".jpg")
                if not wait_for(lambda: preview.is_file(), 15):
                    raise RuntimeError("rtsp_preview_not_received")
                shape = list(cv2.imread(str(preview)).shape)
                last_frame = preview.stat().st_mtime
                terminate(publisher)
                publisher = None
                disconnected = wait_for(lambda: store.camera(camera["id"])["state"] == "disconnected", 12)
                if not disconnected:
                    raise RuntimeError("disconnect_not_reported")
                publisher = publish()
                reconnected = wait_for(
                    lambda: (
                        preview.stat().st_mtime > last_frame
                        and store.camera(camera["id"])["state"] == "connected"
                    ),
                    20,
                )
                if not reconnected:
                    raise RuntimeError("rtsp_reconnect_failed")
                if errors:
                    raise RuntimeError("worker_thread_failed")
                result = {
                    "status": "PASS",
                    "scope": "generated loopback RTSP only; no physical camera or AI accuracy claim",
                    "image": "bluenviron/mediamtx:1.21.2",
                    "preview_shape": shape,
                    "shared_decoder_count": len(worker.sources),
                    "disconnect_observed": disconnected,
                    "reconnect_observed": reconnected,
                    "inference_state": "BLOCKED: no model configured for transport-only probe",
                    "runtime_seconds": round(time.monotonic() - started, 3),
                }
                worker.stop.set()
                thread.join(timeout=12)
                if thread.is_alive():
                    raise RuntimeError("worker_shutdown_timeout")
                return result
    finally:
        if worker:
            worker.stop.set()
        terminate(publisher)
        if thread:
            thread.join(timeout=12)
        if created:
            docker("stop", name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-disposable", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.allow_disposable:
        print(
            json.dumps(
                {"status": "BLOCKED", "reason": "disposable_loopback_infrastructure_authorization_required"}
            )
        )
        return 2
    result = run()
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
