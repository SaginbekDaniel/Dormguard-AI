"""Pinned, optional NONCOMMERCIAL VideoMAE research benchmark; never a production adapter.

Attribution: cliffer1, violence/nonviolence VideoMAE fine-tune, revision below;
base model MCG-NJU/videomae-base-finetuned-kinetics. Both declare CC BY-NC 4.0:
https://creativecommons.org/licenses/by-nc/4.0/ . Weights are unchanged.
Training-data provenance is unknown; no deployment or field-quality approval.
"""

import argparse
import hashlib
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
REPO = "cliffer1/videomae-base-finetuned-kinetics-violence-nonviolence-tuned"
REVISION = "613844b1d2d27f68944619bf91885a9befb101ac"
FILES = {
    "config.json": "59e7a7ba3196e7d7272304b16dc59e15b09b9f17832d76e8cbd9755487afa8ed",
    "preprocessor_config.json": "4bc9b255ff212041bc28127d4fa0ae1630abca52bb2d882f3e4c0b9e5b763f06",
    "README.md": "0d2adb5b283c3c4bd8fd087d5ce85d0c2a6528c7ca6cc6ff65da51645452614f",
    "model.safetensors": "5166b055ebee1439d6517c5bf2d516dce7a1f65afc53367f43b5848970853d53",
}


def fetch(directory):
    """Fixed public artifacts only; verified TLS/digests; never overwrite conflicts."""
    import certifi

    from dormguard.evaluation import sha256

    directory.mkdir(parents=True, exist_ok=True)
    context = ssl.create_default_context(cafile=certifi.where())
    for filename, digest in FILES.items():
        target = directory / filename
        if target.exists():
            if sha256(target) != digest:
                raise ValueError("conflicting_existing_artifact_" + filename)
            continue
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".fetch-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                request = urllib.request.Request(
                    f"https://huggingface.co/{REPO}/resolve/{REVISION}/{filename}"
                )
                sha, total = hashlib.sha256(), 0
                with urllib.request.urlopen(request, context=context, timeout=60) as response:
                    for block in iter(lambda: response.read(1024 * 1024), b""):
                        total += len(block)
                        if total > (350_000_000 if filename == "model.safetensors" else 65536):
                            raise ValueError("artifact_too_large")
                        sha.update(block)
                        stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
                if sha.hexdigest() != digest:
                    raise ValueError("artifact_digest_mismatch")
                os.link(temporary, target)  # exclusive publication; no replacement
            finally:
                temporary.unlink(missing_ok=True)


class ResearchVideoMAE:
    """Strict local safetensors load and pinned official Transformers preprocessing."""

    name = REPO + "@" + REVISION
    frames = 16

    def __init__(self, directory):
        import torch
        from safetensors.torch import load_file
        from transformers import VideoMAEConfig, VideoMAEForVideoClassification, VideoMAEImageProcessor

        from dormguard.evaluation import sha256

        for filename, expected in FILES.items():
            if sha256(directory / filename) != expected:
                raise ValueError("artifact_digest_mismatch")
        config = json.loads((directory / "config.json").read_text())
        if config["id2label"] != {"0": "NonViolence", "1": "Violence"} or config["num_frames"] != 16:
            raise ValueError("unexpected_candidate_labels_or_frames")
        self.torch = torch
        torch.set_num_threads(2)
        torch.manual_seed(42)
        self.module = VideoMAEForVideoClassification(VideoMAEConfig.from_dict(config))
        self.module.load_state_dict(load_file(str(directory / "model.safetensors")), strict=True)
        self.module.eval()
        self.processor = VideoMAEImageProcessor.from_dict(
            json.loads((directory / "preprocessor_config.json").read_text())
        )
        self.preprocess_times, self.inference_times = [], []

    def predict(self, frames):
        import numpy as np

        started = time.perf_counter()
        indices = np.linspace(0, len(frames) - 1, self.frames).round().astype(int)
        rgb = [np.ascontiguousarray(frames[i][:, :, ::-1]) for i in indices]
        inputs = self.processor(images=rgb, return_tensors="pt")
        if tuple(inputs["pixel_values"].shape) != (1, 16, 3, 224, 224):
            raise ValueError("unexpected_candidate_input")
        prepared = time.perf_counter()
        with self.torch.inference_mode():
            logits = self.module(**inputs).logits
        if tuple(logits.shape) != (1, 2) or not bool(self.torch.isfinite(logits).all()):
            raise ValueError("invalid_candidate_logits")
        self.preprocess_times.append(prepared - started)
        self.inference_times.append(time.perf_counter() - prepared)
        return float(self.torch.softmax(logits, dim=1)[0, 1].item())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--noncommercial-evaluation-authorized", action="store_true")
    parser.add_argument("--fetch-public-weights", action="store_true")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.noncommercial_evaluation_authorized:
        parser.error("Explicit noncommercial local evaluation authorization is required")
    if args.fetch_public_weights:
        fetch(args.artifacts)
    from dormguard.evaluation import evaluate_dataset
    from dormguard.telemetry import distribution
    from dormguard.worker import rss_mb

    started = time.perf_counter()
    model = ResearchVideoMAE(args.artifacts)
    loaded = time.perf_counter()
    result = evaluate_dataset(
        args.dataset,
        args.media_root,
        model,
        {
            "sample_fps": 4,
            "window_seconds": 5,
            "stride_seconds": 2.5,
            "confirmations": 2,
            "cooldown_seconds": 60,
            "event_match_grace_seconds": 0,
        },
        [0.5, 0.6, 0.7, 0.8, 0.9],
    )
    result.update(
        artifact_revision=REVISION,
        artifact_sha256=FILES,
        checkpoint_load_seconds=loaded - started,
        peak_process_rss_mb=rss_mb(),
        preprocess_seconds=distribution(model.preprocess_times),
        model_forward_seconds=distribution(model.inference_times),
        rights="CC-BY-NC-4.0 publisher declaration; noncommercial local research only; training-data provenance unknown",
        deployment_selected=False,
        seed=42,
        device="cpu",
        threads=2,
    )
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


if __name__ == "__main__":
    main()
