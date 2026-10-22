"""Opt-in local manifest evaluation. No download, provider request or database write."""

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--model-manifest", type=Path, required=True)
    parser.add_argument("--evaluation-authorized", action="store_true")
    parser.add_argument("--model-rights-confirmed", action="store_true")
    parser.add_argument(
        "--noncommercial-demo",
        action="store_true",
        help="Explicitly enable the reviewed local-only VideoMAE demonstration adapter",
    )
    parser.add_argument("--thresholds", default="0.5,0.6,0.7,0.8,0.9")
    parser.add_argument("--sample-fps", type=float, default=4)
    parser.add_argument("--window-seconds", type=float, default=5)
    parser.add_argument("--stride-seconds", type=float, default=2.5)
    parser.add_argument("--confirmations", type=int, choices=[2, 3], default=2)
    parser.add_argument("--cooldown-seconds", type=float, default=60)
    parser.add_argument("--event-match-grace-seconds", type=float, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.evaluation_authorized or not args.model_rights_confirmed:
        print(
            json.dumps({"status": "BLOCKED", "reason": "evaluation_and_model_rights_authorization_required"})
        )
        return 2
    from dormguard.evaluation import evaluate_dataset, sha256
    from dormguard.model import load_model

    before = time.monotonic()
    adapter = load_model(
        SimpleNamespace(
            model_manifest=args.model_manifest,
            model_rights_confirmed=True,
            model_device="cpu",
            runtime_mode="offline_demo" if args.noncommercial_demo else "evaluation",
            environment="evaluation",
            auth_mode="local",
        )
    )
    model_load_seconds = time.monotonic() - before
    config = {
        "sample_fps": args.sample_fps,
        "window_seconds": args.window_seconds,
        "stride_seconds": args.stride_seconds,
        "confirmations": args.confirmations,
        "cooldown_seconds": args.cooldown_seconds,
        "event_match_grace_seconds": args.event_match_grace_seconds,
    }
    result = evaluate_dataset(
        args.dataset, args.media_root, adapter, config, [float(t) for t in args.thresholds.split(",")]
    )
    result["model_manifest_sha256"] = sha256(args.model_manifest)
    result["model_load_seconds"] = model_load_seconds
    import psutil

    result["rss_mb"] = psutil.Process().memory_info().rss / 1048576
    result["model_manifest"] = json.loads(args.model_manifest.read_text())
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "status": result["status"],
                "runtime_seconds": result["runtime_seconds"],
                "output": str(args.output),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
