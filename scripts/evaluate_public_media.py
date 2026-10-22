"""Reproducible two-film model probes; never a field accuracy benchmark.

Reads reviewed public-domain sources, verifies pinned SHA-256, and uses actual
checkpoint inference. No private source discovery or synthetic model scores.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import statistics
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]


def download(sample, root):
    target = root / sample["file"]
    if target.is_symlink():
        raise ValueError("refusing_symlink")
    if not target.exists():
        import certifi

        request = Request(sample["url"], headers={"User-Agent": "DormGuardLocalEvaluation/0.1"})
        with urlopen(
            request, timeout=30, context=ssl.create_default_context(cafile=certifi.where())
        ) as response:
            data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("public_fixture_size_limit")
        if hashlib.sha256(data).hexdigest() != sample["sha256"]:
            raise ValueError("public_fixture_digest_mismatch")
        with target.open("xb") as handle:
            handle.write(data)
        os.chmod(target, 0o600)
    if hashlib.sha256(target.read_bytes()).hexdigest() != sample["sha256"]:
        raise ValueError("existing_fixture_digest_mismatch_refusing_overwrite")
    return target


def evaluate(adapter, sample, path, threshold):
    import cv2

    from dormguard.worker import TemporalGate

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError("video_open_failed")
    frames = []
    previous = -1
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            stamp = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if stamp <= previous:
                stamp = index / fps
            index += 1
            previous = stamp
            if stamp > sample["evaluation_end_seconds"]:
                break
            if stamp < sample["evaluation_start_seconds"]:
                continue
            if frames and stamp - frames[-1][0] < 0.249:
                continue
            height, width = frame.shape[:2]
            factor = min(640 / width, 360 / height, 1)
            frame = cv2.resize(frame, (int(width * factor), int(height * factor)))
            frames.append((stamp, frame))
    finally:
        capture.release()
    gate = TemporalGate(threshold, 2, 60)
    results = []
    end = sample["evaluation_start_seconds"] + 5
    while end <= sample["evaluation_end_seconds"]:
        window = [(stamp, frame) for stamp, frame in frames if end - 5 <= stamp <= end]
        if len(window) >= 2:
            before = time.perf_counter()
            score = adapter.predict([frame for _, frame in window])
            latency = time.perf_counter() - before
            # Offline event-time replay; only timestamps are simulated, scores are actual inference.
            temporal = gate.observe(score, end, now=end)
            results.append(
                {
                    "start_seconds": end - 5,
                    "end_seconds": end,
                    "sampled_frames": len(window),
                    "score": score,
                    "inference_seconds": round(latency, 6),
                    "above_threshold": score >= threshold,
                    "temporal_trigger": temporal,
                }
            )
        end += 2.5
    if not results:
        raise RuntimeError("no_complete_evaluation_windows")
    detected = any(row["temporal_trigger"] for row in results)
    positive = sample["expected_label"] == "fight_like_positive_proxy"
    outcome = (
        ("positive_proxy_detected" if detected else "missed_positive_proxy")
        if positive
        else ("false_positive" if detected else "negative_no_alert")
    )
    return {
        "id": sample["id"],
        "expected_label": sample["expected_label"],
        "execution_status": "PASS",
        "outcome": outcome,
        "temporal_alert": detected,
        "max_score": max(row["score"] for row in results),
        "min_score": min(row["score"] for row in results),
        "median_inference_seconds": statistics.median(row["inference_seconds"] for row in results),
        "sha256": sample["sha256"],
        "windows": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-authorized", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "test-data/public-evaluation")
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "docs/engineering/MODEL_MANIFEST_EXAMPLE.json"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.evaluation_authorized:
        print(
            json.dumps({"status": "BLOCKED", "reason": "explicit_public_evaluation_authorization_required"})
        )
        return 2
    from types import SimpleNamespace

    from dormguard.model import load_model
    from dormguard.worker import rss_mb

    sources = json.loads((ROOT / "docs/engineering/PUBLIC_EVALUATION_SOURCES.json").read_text())
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = time.perf_counter()
    adapter = load_model(
        SimpleNamespace(model_manifest=args.manifest, model_rights_confirmed=True, model_device="cpu")
    )
    loaded = time.perf_counter()
    results = []
    for sample in sources["samples"]:
        path = args.data_dir / sample["file"]
        if args.download:
            path = download(sample, args.data_dir)
        elif not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sample["sha256"]:
            raise ValueError("reviewed_public_fixture_missing_or_modified")
        results.append(evaluate(adapter, sample, path, 0.7))
    result = {
        "execution_status": "PASS",
        "scope": sources["scope"],
        "model": adapter.name,
        "device": adapter.device,
        "threshold_preregistered": 0.7,
        "temporal_policy": "two of three, five-second windows, 2.5-second stride, 60s cooldown",
        "checkpoint_load_seconds": round(loaded - started, 3),
        "total_seconds": round(time.perf_counter() - started, 3),
        "rss_highwater_mb": round(rss_mb(), 2),
        "samples": results,
        "field_accuracy": "NOT_RUN: two correlated historical films cannot establish domain accuracy",
        "positive_proxy_is_real_dormitory_fight": False,
    }
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
