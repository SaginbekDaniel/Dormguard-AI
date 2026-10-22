"""Bounded real decoder probe. Default creates a non-human transport fixture.

No real camera is opened without --camera and --authorized-camera. Source values
come only from private server environment references and are never printed.
This probe validates transport, not model accuracy or physical CCTV readiness.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def generate_fixture(path: Path, seconds=2, fps=10):
    import cv2
    import numpy as np

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (320, 180))
    if not writer.isOpened():
        raise RuntimeError("fixture_encoder_unavailable")
    try:
        for index in range(round(seconds * fps)):
            frame = np.zeros((180, 320, 3), dtype=np.uint8)
            frame[:, :, 0] = (index * 11) % 255
            cv2.rectangle(frame, ((index * 7) % 240, 40), ((index * 7) % 240 + 60, 110), (20, 190, 80), -1)
            cv2.putText(
                frame,
                "GENERATED TRANSPORT FIXTURE",
                (10, 160),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.43,
                (255, 255, 255),
                1,
            )
            writer.write(frame)
    finally:
        writer.release()
    return path


def probe(source, seconds=5):
    from dormguard.worker import open_capture

    started = time.monotonic()
    capture = open_capture(source)
    frames, shape = 0, None
    try:
        if not capture.isOpened():
            raise RuntimeError("decoder_open_failed")
        while time.monotonic() - started < seconds:
            ok, frame = capture.read()
            if not ok:
                break
            frames += 1
            shape = list(frame.shape)
        if not frames:
            raise RuntimeError("no_frames_decoded")
    finally:
        capture.release()
    return {
        "status": "PASS",
        "decoded_frames": frames,
        "last_frame_shape": shape,
        "runtime_seconds": round(time.monotonic() - started, 3),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera")
    parser.add_argument("--authorized-camera", action="store_true")
    parser.add_argument("--seconds", type=int, default=5)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 60:
        parser.error("seconds must be 1..60")
    try:
        if args.camera:
            if not args.authorized_camera:
                print(json.dumps({"status": "BLOCKED", "reason": "camera_authorization_required"}))
                return 2
            from dormguard.config import load_settings
            from dormguard.security import resolve_source
            from dormguard.store import Store

            settings = load_settings()
            camera = Store(settings.data_dir, settings.database_url).camera(args.camera, private=True)
            result = probe(resolve_source(settings, camera), args.seconds)
            result["scope"] = "authorized configured source; reconnect and capacity NOT_RUN"
        else:
            with tempfile.TemporaryDirectory(prefix="dormguard-transport-") as temporary:
                source = generate_fixture(Path(temporary) / "generated.avi")
                result = probe(str(source), args.seconds)
                result["scope"] = (
                    "generated non-human prerecorded transport fixture; physical RTSP NOT_RUN; AI NOT_RUN"
                )
        print(json.dumps(result, indent=2))
        return 0
    except ImportError:
        print(json.dumps({"status": "BLOCKED", "reason": "opencv_runtime_missing"}))
        return 2
    except Exception as error:
        print(json.dumps({"status": "FAIL", "reason": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
