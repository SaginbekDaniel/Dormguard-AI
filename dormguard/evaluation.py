"""Offline, bounded, hash-pinned evaluation. Never imported by the live detector.

Annotations describe expected suspicion, not verified wrongdoing. Cohorts are
reported separately. Overlapping windows are correlated descriptive observations.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import time
from collections import deque
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .telemetry import distribution
from .worker import FrameSampler, TemporalGate, open_capture


def runtime_versions():
    versions = {}
    for package in (
        "torch",
        "torchvision",
        "timm",
        "safetensors",
        "Pillow",
        "opencv-python-headless",
        "numpy",
    ):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = "NOT_INSTALLED"
    return versions


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def wilson(successes, total):
    """Descriptive 95% binomial interval; independence is a dataset prerequisite."""
    if not total:
        return None
    z = 1.95996398454
    p = successes / total
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [max(0, center - radius), min(1, center + radius)]


def load_dataset(path, media_root):
    path, root = Path(path), Path(media_root).resolve()
    data = json.loads(path.read_text())
    if data.get("version") != 1 or not isinstance(data.get("samples"), list) or not data["samples"]:
        raise ValueError("invalid_dataset_manifest")
    seen, intervals = set(), {}
    for sample in data["samples"]:
        for key in (
            "id",
            "file",
            "sha256",
            "camera_id",
            "scene_id",
            "scenario",
            "cohort",
            "provenance",
            "authorization",
        ):
            if not isinstance(sample.get(key), str) or not sample[key].strip():
                raise ValueError("missing_sample_" + key)
        if sample["id"] in seen or sample["cohort"] not in {
            "infrastructure_smoke",
            "proxy_behavior",
            "representative_field",
        }:
            raise ValueError("duplicate_id_or_invalid_cohort")
        seen.add(sample["id"])
        source = (root / sample["file"]).resolve()
        if not source.is_relative_to(root) or source.is_symlink() or not source.is_file():
            raise ValueError("media_outside_root_or_missing")
        if sha256(source) != sample["sha256"]:
            raise ValueError("media_digest_mismatch")
        start, end = sample.get("start_seconds"), sample.get("end_seconds")
        if (
            not all(type(x) in (int, float) and math.isfinite(x) for x in (start, end))
            or not 0 <= start < end
        ):
            raise ValueError("invalid_sample_bounds")
        # Never count overlapping portions of one recording as extra camera hours.
        for first, last in intervals.setdefault(sample["sha256"], []):
            if start < last and end > first:
                raise ValueError("overlapping_recording_coverage")
        intervals[sample["sha256"]].append((start, end))
        events = sample.get("events")
        if not isinstance(events, list):
            raise ValueError("explicit_complete_event_annotations_required")
        previous = start
        for event in events:
            first, last = event.get("start_seconds"), event.get("end_seconds")
            if (
                not all(type(x) in (int, float) and math.isfinite(x) for x in (first, last))
                or not previous <= first < last <= end
            ):
                raise ValueError("invalid_or_overlapping_event_annotation")
            previous = last
    return data


def evaluate_video(adapter, sample, path, config):
    import cv2

    ring = deque()
    rows, insufficient_coverage = [], []
    capture = open_capture(str(path))
    sampler = FrameSampler(config["sample_fps"], sample["start_seconds"])
    next_window = sample["start_seconds"] + config["window_seconds"]
    previous, index, decoded = -1, 0, 0
    try:
        if not capture.isOpened():
            raise ValueError("evaluation_video_open_failed")
        fps = capture.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("unknown_video_fps")
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            decoded += 1
            stamp = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if not math.isfinite(stamp) or stamp <= previous:
                stamp = index / fps
            index += 1
            if stamp <= previous:
                raise ValueError("nonmonotonic_video_time")
            previous = stamp
            if stamp > sample["end_seconds"]:
                break
            if not sampler.accept(stamp):
                continue
            h, w = frame.shape[:2]
            scale = min(640 / w, 360 / h, 1)
            frame = cv2.resize(frame, (max(2, int(w * scale)), max(2, int(h * scale))))
            ring.append((stamp, frame))
            while ring and ring[0][0] < stamp - config["window_seconds"] - 1 / config["sample_fps"]:
                ring.popleft()
            if stamp + 1 / fps / 2 < next_window:
                continue
            frames = [(t, f) for t, f in ring if t >= stamp - config["window_seconds"] - 1e-6]
            if (
                len(frames) < 2
                or frames[-1][0] - frames[0][0] < config["window_seconds"] - 1 / config["sample_fps"]
            ):
                insufficient_coverage.append(stamp)
                continue  # Recorded explicitly; wait for a complete fresh window.
            before = time.perf_counter()
            score = adapter.predict([f for _, f in frames])
            elapsed = time.perf_counter() - before
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("invalid_model_score")
            rows.append(
                {
                    "start_seconds": frames[0][0],
                    "end_seconds": stamp,
                    "score": score,
                    "sampled_frames": len(frames),
                    "sample_timestamps": [t for t, _ in frames],
                    "inference_seconds": elapsed,
                }
            )
            next_window += config["stride_seconds"]
        if previous + 1 / fps < sample["end_seconds"]:
            raise ValueError("truncated_or_corrupt_evaluation_video")
        if not rows:
            raise ValueError("no_complete_windows")
        return {
            "id": sample["id"],
            "decoded_frames": decoded,
            "source_fps": fps,
            "windows": rows,
            "insufficient_coverage_timestamps": insufficient_coverage,
        }
    finally:
        capture.release()


def decisions(sample, predictions, threshold, config):
    gate = TemporalGate(
        threshold,
        config["confirmations"],
        config["cooldown_seconds"],
        max(config["window_seconds"], 2.1 * config["stride_seconds"]),
    )
    events = sample["events"]
    rows, triggers, first = [], [], []
    grace = config.get("event_match_grace_seconds", 0)
    for row in predictions["windows"]:
        end, start, score = row["end_seconds"], row["start_seconds"], row["score"]
        positive = any(start < event["end_seconds"] and end >= event["start_seconds"] for event in events)
        trigger = gate.observe(score, end, now=end)
        rows.append(
            {
                **row,
                "expected_positive": positive,
                "above_threshold": score >= threshold,
                "temporal_trigger": trigger,
            }
        )
        if score >= threshold:
            first.append(end)
        if trigger:
            triggers.append(end)
    matches = []
    for event in events:
        low, high = event["start_seconds"], event["end_seconds"] + grace
        first_detection = next((t for t in first if low <= t <= high), None)
        confirmation = next((t for t in triggers if low <= t <= high), None)
        matches.append(
            {
                **event,
                "detected": confirmation is not None,
                "time_to_first_detection_seconds": None if first_detection is None else first_detection - low,
                "time_to_confirmed_incident_seconds": None if confirmation is None else confirmation - low,
            }
        )
    false_alarms = [
        t for t in triggers if not any(e["start_seconds"] <= t <= e["end_seconds"] + grace for e in events)
    ]
    return {
        "id": sample["id"],
        "cohort": sample["cohort"],
        "camera_id": sample["camera_id"],
        "scene_id": sample["scene_id"],
        "scenario": sample["scenario"],
        "occlusion": sample.get("occlusion", "unspecified"),
        "duration_seconds": sample["end_seconds"] - sample["start_seconds"],
        "windows": rows,
        "events": matches,
        "false_alarm_timestamps": false_alarms,
        "insufficient_coverage_timestamps": predictions.get("insufficient_coverage_timestamps", []),
        "source_fps": predictions.get("source_fps"),
    }


def summarize(samples):
    rows = [r for s in samples for r in s["windows"]]
    counts = {key: 0 for key in ("tp", "fp", "tn", "fn")}
    for row in rows:
        truth, predicted = row["expected_positive"], row["above_threshold"]
        counts[("tp" if predicted else "fn") if truth else ("fp" if predicted else "tn")] += 1
    tp, fp, tn, fn = (counts[k] for k in ("tp", "fp", "tn", "fn"))
    events = [e for s in samples for e in s["events"]]
    hits = sum(e["detected"] for e in events)
    hours = sum(s["duration_seconds"] for s in samples) / 3600
    false_alarms = sum(len(s["false_alarm_timestamps"]) for s in samples)
    return {
        "clips": len(samples),
        "scenes": len({s["scene_id"] for s in samples}),
        "windows": len(rows),
        "confusion": counts,
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "f1": ratio(2 * tp, 2 * tp + fp + fn),
        "false_positive_rate": ratio(fp, fp + tn),
        "false_negative_rate": ratio(fn, fn + tp),
        "annotated_events": len(events),
        "detected_events": hits,
        "event_recall": ratio(hits, len(events)),
        "event_recall_wilson95_descriptive": wilson(hits, len(events)),
        "evaluated_camera_hours": hours,
        "false_alarms": false_alarms,
        "false_alarms_per_camera_hour": ratio(false_alarms, hours),
        "score_distribution": distribution([r["score"] for r in rows]),
        "inference_seconds": distribution([r["inference_seconds"] for r in rows]),
        "time_to_first_detection_seconds": distribution(
            [
                e["time_to_first_detection_seconds"]
                for e in events
                if e["time_to_first_detection_seconds"] is not None
            ]
        ),
        "time_to_confirmation_seconds": distribution(
            [
                e["time_to_confirmed_incident_seconds"]
                for e in events
                if e["time_to_confirmed_incident_seconds"] is not None
            ]
        ),
        "caution": "Overlapping windows and related scenes are correlated; descriptive intervals do not establish field accuracy. Zero observed alarms is not a zero risk guarantee.",
    }


def evaluate_dataset(dataset_path, media_root, adapter, config, thresholds):
    for key, low, high in (
        ("sample_fps", 0.2, 15),
        ("window_seconds", 1, 30),
        ("stride_seconds", 0.2, 30),
        ("cooldown_seconds", 0, 3600),
        ("event_match_grace_seconds", 0, 30),
    ):
        value = config.get(key, 0)
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError("invalid_configuration_" + key)
    if config["confirmations"] not in (2, 3) or config["window_seconds"] * config["sample_fps"] < 2:
        raise ValueError("invalid_temporal_or_window_configuration")
    if not thresholds:
        raise ValueError("thresholds_required")
    data = load_dataset(dataset_path, media_root)
    started = time.perf_counter()
    predictions = {
        s["id"]: evaluate_video(adapter, s, Path(media_root) / s["file"], config) for s in data["samples"]
    }
    sweeps = []
    for threshold in thresholds:
        if not math.isfinite(threshold) or not 0 < threshold < 1:
            raise ValueError("invalid_threshold")
        samples = [decisions(s, predictions[s["id"]], threshold, config) for s in data["samples"]]
        cohorts = {}
        for cohort in sorted({s["cohort"] for s in samples}):
            subset = [s for s in samples if s["cohort"] == cohort]
            cohorts[cohort] = {
                "metrics": summarize(subset),
                "by_scenario": {
                    key: summarize([s for s in subset if s["scenario"] == key])
                    for key in sorted({s["scenario"] for s in subset})
                },
                "by_occlusion": {
                    key: summarize([s for s in subset if s["occlusion"] == key])
                    for key in sorted({s["occlusion"] for s in subset})
                },
                "samples": subset,
            }
        sweeps.append({"threshold": threshold, "cohorts": cohorts})
    return {
        "status": "PASS",
        "scope": "offline annotation-based replay; event time is media time, not live alert latency",
        "dataset_sha256": sha256(dataset_path),
        "dataset_manifest": data,
        "model": adapter.name,
        "configuration": config,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "packages": runtime_versions(),
        },
        "runtime_seconds": time.perf_counter() - started,
        "sweeps": sweeps,
        "production_threshold_changed": False,
    }
