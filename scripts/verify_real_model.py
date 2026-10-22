"""Execute a pinned real checkpoint without fabricating a positive incident.

Default exits BLOCKED. --generated-fixture runs actual inference on a generated
non-human clip solely to exercise the adapter; it establishes no detection
accuracy. Actual footage requires --authorized-footage and a local file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(manifest, clip, rights_confirmed, device):
    import cv2

    from dormguard.model import load_model
    from dormguard.worker import open_capture, rss_mb

    started = time.monotonic()
    adapter = load_model(
        SimpleNamespace(model_manifest=manifest, model_rights_confirmed=rights_confirmed, model_device=device)
    )
    loaded = time.monotonic()
    capture = open_capture(str(clip))
    frames = []
    try:
        if not capture.isOpened():
            raise RuntimeError("clip_open_failed")
        # Bound verifier input. The worker uses its own sampled rolling windows.
        for _ in range(300):
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(cv2.resize(frame, (320, 180)))
    finally:
        capture.release()
    if len(frames) < 2:
        raise RuntimeError("clip_too_short")
    before = time.monotonic()
    score = adapter.predict(frames)
    finished = time.monotonic()
    return {
        "status": "PASS",
        "scope": "real pretrained checkpoint execution only",
        "model": adapter.name,
        "device": adapter.device,
        "decoded_frames": len(frames),
        "uncalibrated_suspicion_score": score,
        "checkpoint_load_seconds": round(loaded - started, 3),
        "inference_seconds": round(finished - before, 3),
        "runtime_seconds": round(finished - started, 3),
        "rss_mb": round(rss_mb(), 2),
        "clip_sha256": hashlib.sha256(Path(clip).read_bytes()).hexdigest(),
        "positive_incident_workflow": "BLOCKED: requires authorized representative positive footage and model validation",
        "accuracy": "NOT_RUN",
        "telegram_delivery": "NOT_RUN",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--rights-confirmed", action="store_true")
    parser.add_argument("--clip", type=Path)
    parser.add_argument("--authorized-footage", action="store_true")
    parser.add_argument("--generated-fixture", action="store_true")
    parser.add_argument("--device", choices=("cpu", "cuda", "auto"), default="cpu")
    args = parser.parse_args(argv)
    if not args.manifest or not args.rights_confirmed:
        print(
            json.dumps(
                {"status": "BLOCKED", "reason": "pinned_manifest_and_model_rights_confirmation_required"}
            )
        )
        return 2
    if args.clip and args.generated_fixture:
        parser.error("select a clip or a generated fixture")
    if not args.generated_fixture and not (args.clip and args.authorized_footage):
        print(json.dumps({"status": "BLOCKED", "reason": "authorized_local_clip_required"}))
        return 2
    try:
        if args.generated_fixture:
            from verify_rtsp import generate_fixture

            with tempfile.TemporaryDirectory(prefix="dormguard-real-model-") as temporary:
                clip = generate_fixture(Path(temporary) / "generated.avi")
                result = run(args.manifest, clip, args.rights_confirmed, args.device)
                result["fixture"] = "generated non-human colored shapes; not positive AI evidence"
        else:
            if not args.clip.is_file() or args.clip.suffix.lower() not in (".mp4", ".avi", ".mov"):
                raise ValueError("invalid_local_clip")
            result = run(args.manifest, args.clip, args.rights_confirmed, args.device)
            result["fixture"] = "explicitly authorized local video"
        print(json.dumps(result, indent=2))
        return 0
    except ImportError:
        print(json.dumps({"status": "BLOCKED", "reason": "model_or_video_runtime_missing"}))
        return 2
    except Exception as error:
        from dormguard.model import ModelBlocked

        blocked = isinstance(error, ModelBlocked)
        print(
            json.dumps(
                {
                    "status": "BLOCKED" if blocked else "FAIL",
                    "reason": str(error) if blocked else type(error).__name__,
                }
            )
        )
        return 2 if blocked else 1


if __name__ == "__main__":
    raise SystemExit(main())
