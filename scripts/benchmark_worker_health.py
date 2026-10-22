"""Repeated disposable slow-health-SQL transport trials, never camera/AI acceptance."""

import argparse
import importlib.util
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dormguard.config import Settings  # noqa: E402
from dormguard.store import Store  # noqa: E402
from dormguard.telemetry import distribution  # noqa: E402
from dormguard.worker import Worker  # noqa: E402
from scripts.verify_rtsp import generate_fixture  # noqa: E402


class ControlledNegativeModelDouble:
    name = "CONTROLLED_NEGATIVE_HEALTH_BENCHMARK_DOUBLE"

    def predict(self, frames):
        return 0.1


class DelayedHealthStore:
    """Real disposable SQLite, with an explicitly artificial health-only delay."""

    def __init__(self, store, delay):
        self.store, self.delay = store, delay
        self.calls = 0
        self.durations = []

    def cameras(self, private=False):
        return self.store.cameras(private=private)

    def _write(self, method, *args, **kwargs):
        started = time.monotonic()
        time.sleep(self.delay)
        result = method(*args, **kwargs)
        self.calls += 1
        self.durations.append(time.monotonic() - started)
        return result

    def set_camera_health(self, *args, **kwargs):
        return self._write(self.store.set_camera_health, *args, **kwargs)

    def heartbeat(self, *args, **kwargs):
        return self._write(self.store.heartbeat, *args, **kwargs)


def profile(worker_class, count, seconds, delay):
    with tempfile.TemporaryDirectory(prefix="dormguard-health-benchmark-") as temporary:
        root = Path(temporary)
        settings = Settings(root, max_cameras=count, window_seconds=1, window_stride=0.5)
        store = Store(root)
        owner = store.provision_user("director@example.invalid", "director")
        source_dir = root / "test_sources"
        source_dir.mkdir(mode=0o700)
        references = {}
        for index in range(count):
            path = generate_fixture(source_dir / f"generated-{index}.avi", seconds=seconds, fps=10)
            reference = f"DORMGUARD_CAMERA_HEALTH_BENCH_{index}"
            references[reference] = str(path)
            store.create_camera(f"Generated {index}", "No people", "test", reference, owner["id"])
        delayed = DelayedHealthStore(store, delay)
        worker = worker_class(settings, delayed, ControlledNegativeModelDouble())
        started = time.monotonic()
        try:
            with patch.dict(os.environ, references):
                worker.run(once=True)
            elapsed = time.monotonic() - started
            publisher = getattr(worker, "health_publisher", None)
            return {
                "runtime_seconds": elapsed,
                "source_count": count,
                "source_seconds": seconds,
                "health_sql_calls": delayed.calls,
                "health_sql_seconds": distribution(delayed.durations),
                "health_persistence": publisher.snapshot() if publisher else None,
                "inferred_windows": worker.inferred,
                "worker_errors": worker.errors,
                "queue_drops": worker.dropped,
                "per_camera": {
                    key: {
                        "decoded_frames": value.decoded_frames,
                        "sampled_frames": value.sampled_frames,
                        "analyzed_windows": value.analyzed_windows,
                        "sampled_fps_over_run": value.sampled_frames / elapsed,
                    }
                    for key, value in worker.states.items()
                },
                "timings": worker.metrics.snapshot(),
                "audit_integrity": store.verify_audit()["valid"],
                "incidents": len(store.incidents()),
            }
        finally:
            store.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-disposable", action="store_true")
    parser.add_argument("--baseline-worker", type=Path)
    parser.add_argument("--counts", default="1,2,4,8")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=1.5)
    parser.add_argument("--health-delay", type=float, default=0.35)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    counts = [int(value) for value in args.counts.split(",")]
    if (
        not args.allow_disposable
        or not counts
        or any(value not in {1, 2, 4, 8} for value in counts)
        or not 1 <= args.repeats <= 5
        or not 1 <= args.seconds <= 10
        or not 0 <= args.health_delay <= 0.5
    ):
        parser.error("Explicit disposable consent and bounded 1/2/4/8 profiles required")
    worker_class = Worker
    if args.baseline_worker:
        specification = importlib.util.spec_from_file_location(
            "dormguard._acceptance_before_worker", args.baseline_worker
        )
        module = importlib.util.module_from_spec(specification)
        sys.modules[specification.name] = module
        specification.loader.exec_module(module)
        worker_class = module.Worker
    started = time.monotonic()
    runs = [
        profile(worker_class, count, args.seconds, args.health_delay)
        for count in counts
        for _ in range(args.repeats)
    ]
    passed = all(row["audit_integrity"] and not row["worker_errors"] and not row["incidents"] for row in runs)
    result = {
        "status": "PASS" if passed else "FAIL",
        "scope": "Actual generated-file decode, disposable SQLite and labeled negative model double. Artificial health latency, not WAN/RTSP/physical-camera/model capacity, accuracy or provider delivery.",
        "environment": {"platform": platform.platform(), "python": platform.python_version()},
        "baseline_worker": bool(args.baseline_worker),
        "artificial_health_delay_seconds": args.health_delay,
        "runtime_seconds": time.monotonic() - started,
        "runs": runs,
        "runtime_distributions_by_source_count": {
            str(count): distribution([row["runtime_seconds"] for row in runs if row["source_count"] == count])
            for count in counts
        },
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "runs": len(runs), "output": str(args.output)}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
