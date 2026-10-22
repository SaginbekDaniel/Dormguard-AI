"""Bounded disposable prerecorded-camera capacity; no network cameras or messages.

Distinct generated files exercise distinct decoders, but depict the same generated
scene. This is an infrastructure benchmark, never field detection validation.
"""

import argparse
import json
import os
import platform
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ControlledSlowNegativeDouble:
    """Explicit overload-only test double. No production worker imports this."""

    name = "CONTROLLED_SLOW_NEGATIVE_TEST_DOUBLE"

    def predict(self, frames):
        time.sleep(0.8)
        return 0.1


def profile(count, seconds, model):
    import psutil

    from dormguard.config import Settings
    from dormguard.store import Store
    from dormguard.worker import Worker
    from scripts.verify_rtsp import generate_fixture

    with tempfile.TemporaryDirectory(prefix="dormguard-capacity-") as directory:
        settings = Settings(Path(directory), max_cameras=count)
        store = Store(settings.data_dir)
        director = store.provision_user("director@example.invalid", "director")
        sources = settings.data_dir / "test_sources"
        sources.mkdir(mode=0o700)
        refs = {}
        for index in range(count):
            source = generate_fixture(sources / f"generated-{index}.avi", seconds=seconds, fps=10)
            ref = f"DORMGUARD_CAMERA_BENCHMARK_{index}"
            refs[ref] = str(source)
            store.create_camera(f"Generated {index}", "Disposable benchmark", "test", ref, director["id"])
        worker = Worker(settings, store, model)
        meter = psutil.Process()
        meter.cpu_percent()
        peak_rss, peak_queue, cpu, failures = 0, 0, [], []

        def run():
            try:
                worker.run(once=True)
            except Exception as error:
                failures.append(type(error).__name__)

        started = time.monotonic()
        with patch.dict(os.environ, refs):
            thread = threading.Thread(target=run)
            thread.start()
            while thread.is_alive():
                peak_rss = max(peak_rss, meter.memory_info().rss / 1024**2)
                peak_queue = max(peak_queue, worker.queue.qsize())
                cpu.append(meter.cpu_percent())
                if time.monotonic() - started > seconds + 40:
                    worker.stop.set()
                    failures.append("bounded_runtime_exceeded")
                thread.join(timeout=0.2)
        elapsed = time.monotonic() - started
        per_camera = {
            camera_id: {
                "decoded_frames": state.decoded_frames,
                "sampled_frames": state.sampled_frames,
                "skipped_windows": state.skipped_windows,
                "analysis_windows": state.analyzed_windows,
                "decode_fps_over_run": state.decoded_frames / elapsed,
                "sample_fps_over_run": state.sampled_frames / elapsed,
                "analysis_windows_per_second": state.analyzed_windows / elapsed,
                "analysis_state": state.analysis,
            }
            for camera_id, state in worker.states.items()
        }
        passed = (
            not failures
            and not worker.errors
            and len(worker.sources) == count
            and all(v["analysis_windows"] > 0 for v in per_camera.values())
            and peak_queue <= settings.max_inference_queue
        )
        return {
            "status": "PASS" if passed else "FAIL",
            "source_count": count,
            "distinct_decoders": len(worker.sources),
            "generated_source_seconds": seconds,
            "source_fps": 10,
            "analysis_sample_fps_configured": settings.inference_fps,
            "model": model.name,
            "runtime_seconds": elapsed,
            "peak_rss_mb_current": peak_rss,
            "cpu_percent_process": {
                "max": max(cpu),
                "mean": sum(cpu) / len(cpu),
                "basis": "100 percent = one logical CPU",
            },
            "queue_capacity": settings.max_inference_queue,
            "peak_queue_depth_observed": peak_queue,
            "inferred_windows": worker.inferred,
            "dropped_windows": worker.dropped,
            "worker_errors": worker.errors,
            "failures": failures,
            "timings": worker.metrics.snapshot(),
            "per_camera": per_camera,
            "incidents": len(store.incidents()),
            "gpu": "NOT_RUN: CPU profile",
            "physical_camera_capacity": "NOT_RUN",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-disposable", action="store_true")
    parser.add_argument("--model-manifest", type=Path)
    parser.add_argument("--model-rights-confirmed", action="store_true")
    parser.add_argument("--controlled-overload", action="store_true")
    parser.add_argument("--counts", default="1,2,4,8")
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.allow_disposable or (not args.controlled_overload and not args.model_rights_confirmed):
        print(
            json.dumps({"status": "BLOCKED", "reason": "explicit_disposable_scope_and_model_rights_required"})
        )
        return 2
    counts = [int(n) for n in args.counts.split(",")]
    if not counts or any(n not in (1, 2, 4, 8) for n in counts) or not 10 <= args.seconds <= 300:
        parser.error("counts must be 1,2,4,8 and seconds 10..300")
    from types import SimpleNamespace

    import psutil

    from dormguard.model import load_model

    model = (
        ControlledSlowNegativeDouble()
        if args.controlled_overload
        else load_model(
            SimpleNamespace(
                model_manifest=args.model_manifest, model_rights_confirmed=True, model_device="cpu"
            )
        )
    )
    result = {
        "scope": "generated duplicated-scene files, distinct paced decoders; infrastructure only",
        "hardware": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "logical_cpus": psutil.cpu_count(),
            "physical_cpus": psutil.cpu_count(logical=False),
            "memory_bytes": psutil.virtual_memory().total,
        },
        "controlled_model_double": args.controlled_overload,
        "profiles": [],
    }
    for count in counts:
        measured = profile(count, args.seconds, model)
        result["profiles"].append(measured)
        print(
            json.dumps(
                {
                    k: measured[k]
                    for k in (
                        "status",
                        "source_count",
                        "runtime_seconds",
                        "inferred_windows",
                        "dropped_windows",
                    )
                }
            ),
            flush=True,
        )
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 0 if all(p["status"] == "PASS" for p in result["profiles"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
