"""Replaceable, fail-closed real inference boundary; no built-in model or scores.

Supports the pinned local ViT safetensors and trusted, rights-cleared TorchScript
artifacts. A manifest pins exact preprocessing/provenance; it is not legal clearance.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


class ModelBlocked(RuntimeError):
    """Inference is unavailable; capture may continue independently."""


class ModelAdapter(Protocol):
    name: str
    frames: int

    def predict(self, frames: Sequence[object]) -> float:
        """Return an uncalibrated suspicion score, never a disciplinary decision."""


@dataclass(frozen=True)
class ModelManifest:
    name: str
    checkpoint: Path
    sha256: str
    frames: int
    size: int
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    labels: tuple[str, ...]
    suspected_index: int
    source_url: str
    license: str
    rights_record: str

    @classmethod
    def read(cls, path: Path, rights_confirmed: bool = False) -> "ModelManifest":
        if not rights_confirmed:
            raise ModelBlocked("model_rights_unconfirmed")
        if not path or not Path(path).is_file():
            raise ModelBlocked("model_manifest_missing")
        try:
            path = Path(path).resolve()
            if path.stat().st_size > 65536:
                raise ValueError("oversize manifest")
            data = json.loads(path.read_text())
            if data.get("format") != "torchscript" or data.get("input_layout") != "NCTHW":
                raise ValueError("unsupported model format or layout")
            if data.get("output") != "logits" or data.get("color") != "RGB":
                raise ValueError("unsupported output or color")
            labels = tuple(data["labels"])
            index = data["suspected_index"]
            frames, size = data["frames"], data["size"]
            mean, std = tuple(data["mean"]), tuple(data["std"])
            if (
                type(index) is not int
                or type(frames) is not int
                or type(size) is not int
                or not 2 <= frames <= 128
                or not 32 <= size <= 512
                or not 2 <= len(labels) <= 100
                or not 0 <= index < len(labels)
                or not all(isinstance(x, str) and x for x in labels)
                or len(set(labels)) != len(labels)
                or len(mean) != 3
                or len(std) != 3
                or not all(math.isfinite(float(x)) for x in (*mean, *std))
                or not all(float(x) > 0 for x in std)
            ):
                raise ValueError("invalid preprocessing")
            if not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
                raise ValueError("invalid digest")
            for key in ("name", "source_url", "license", "rights_record"):
                if not isinstance(data.get(key), str) or not data[key].strip():
                    raise ValueError("missing provenance")
            checkpoint = (path.parent / data["checkpoint"]).resolve()
            if not checkpoint.is_file():
                raise ModelBlocked("model_checkpoint_missing")
            digest = hashlib.sha256()
            with checkpoint.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != data["sha256"]:
                raise ModelBlocked("model_digest_mismatch")
            return cls(
                data["name"],
                checkpoint,
                data["sha256"],
                frames,
                size,
                mean,
                std,
                labels,
                index,
                data["source_url"],
                data["license"],
                data["rights_record"],
            )
        except ModelBlocked:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise ModelBlocked("model_manifest_invalid") from None


class TorchScriptAdapter:
    """Loads one pinned local checkpoint, with CPU fallback when CUDA is absent.

    TorchScript is executable code: provision only a reviewed trusted artifact.
    No download or arbitrary remote model code is executed by this module.
    """

    def __init__(self, manifest: ModelManifest, device: str = "cpu") -> None:
        try:
            import torch
        except ImportError:
            raise ModelBlocked("torch_runtime_missing") from None
        if device not in ("cpu", "cuda", "auto"):
            raise ModelBlocked("model_device_invalid")
        self.torch = torch
        self.manifest = manifest
        self.name, self.frames = manifest.name, manifest.frames
        self.device = "cuda" if device in ("cuda", "auto") and torch.cuda.is_available() else "cpu"
        torch.set_num_threads(2)
        try:
            self.module = torch.jit.load(str(manifest.checkpoint), map_location=self.device).eval()
        except Exception:
            raise ModelBlocked("model_checkpoint_incompatible") from None

    def predict(self, frames: Sequence[object]) -> float:
        if len(frames) < 2:
            raise ValueError("insufficient_frames")
        import cv2
        import numpy as np

        m, torch = self.manifest, self.torch
        indices = np.linspace(0, len(frames) - 1, m.frames).round().astype(int)
        rgb = np.stack(
            [cv2.cvtColor(cv2.resize(frames[i], (m.size, m.size)), cv2.COLOR_BGR2RGB) for i in indices]
        )
        tensor = torch.from_numpy(rgb).float().div_(255).permute(3, 0, 1, 2)
        mean = torch.tensor(m.mean).view(3, 1, 1, 1)
        std = torch.tensor(m.std).view(3, 1, 1, 1)
        tensor = ((tensor - mean) / std).unsqueeze(0).to(self.device)
        with torch.inference_mode():
            logits = self.module(tensor)
        if not isinstance(logits, torch.Tensor) or tuple(logits.shape) != (1, len(m.labels)):
            raise ValueError("model_output_shape_invalid")
        if not bool(torch.isfinite(logits).all()):
            raise ValueError("model_output_nonfinite")
        score = float(torch.softmax(logits, dim=1)[0, m.suspected_index].item())
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("model_score_invalid")
        return score


class LocalViTAdapter:
    """Local safetensors image classifier; average sampled frame scores.

    This is frame-based content screening. Averaging does not add learned motion
    understanding or establish fight-detection quality. No network or remote
    repository code is used while loading or predicting.
    """

    def __init__(self, path: Path, rights_confirmed: bool, device: str):
        if not rights_confirmed:
            raise ModelBlocked("model_rights_unconfirmed")
        try:
            data = json.loads(path.read_text())
            if (
                data.get("format") != "timm_vit_safetensors"
                or type(data.get("frames")) is not int
                or data.get("frames") not in range(2, 17)
                or not isinstance(data.get("name"), str)
                or not data["name"].strip()
                or data.get("architecture") != "vit_base_patch16_224"
                or data.get("labels") != ["non_violence", "violence"]
                or data.get("suspected_index") != 1
                or data.get("aggregation") != "mean_frame_softmax"
                or not all(
                    data.get(k)
                    for k in ("source_url", "revision", "license", "rights_record", "label_mapping_source")
                )
            ):
                raise ValueError("invalid manifest")
            directory = (path.parent / data["checkpoint_dir"]).resolve()
            files = data["files"]
            if set(files) != {"config.json", "preprocessor_config.json", "model.safetensors"}:
                raise ValueError("missing digest")
            for filename, digest in files.items():
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("invalid digest")
                sha = hashlib.sha256()
                with (directory / filename).open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        sha.update(chunk)
                if sha.hexdigest() != digest:
                    raise ModelBlocked("model_digest_mismatch")
            config = json.loads((directory / "config.json").read_text())
            preprocessor = json.loads((directory / "preprocessor_config.json").read_text())
            if (
                config.get("architectures") != ["ViTForImageClassification"]
                or config.get("num_labels") != 2
                or config.get("image_size") != 224
                or preprocessor.get("size") != {"height": 224, "width": 224}
                or preprocessor.get("image_mean") != [0.5] * 3
                or preprocessor.get("image_std") != [0.5] * 3
                or preprocessor.get("resample") != 2
                or preprocessor.get("do_normalize") is not True
                or preprocessor.get("do_rescale") is not True
                or preprocessor.get("do_resize") is not True
                or abs(preprocessor.get("rescale_factor", 0) - 1 / 255) > 1e-12
            ):
                raise ModelBlocked("model_preprocessing_incompatible")
            if device not in ("cpu", "cuda", "auto"):
                raise ValueError("invalid device")
        except ModelBlocked:
            raise
        except (OSError, KeyError, TypeError, ValueError):
            raise ModelBlocked("model_manifest_invalid") from None
        try:
            import timm
            import torch
            from safetensors.torch import load_file
        except ImportError:
            raise ModelBlocked("vit_runtime_missing") from None
        self.torch, self.name, self.frames = torch, data["name"], data["frames"]
        self.device = "cuda" if device in ("auto", "cuda") and torch.cuda.is_available() else "cpu"
        torch.set_num_threads(2)
        try:
            # The publisher's HF config names a Transformers ViT, but the
            # actual artifact contains timm keys. Strict loading is mandatory:
            # from_pretrained would silently leave random initialized weights.
            self.module = timm.create_model(data["architecture"], pretrained=False, num_classes=2)
            self.module.load_state_dict(
                load_file(str(directory / "model.safetensors"), device="cpu"), strict=True
            )
            self.module = self.module.eval().to(self.device)
        except Exception:
            raise ModelBlocked("model_checkpoint_incompatible") from None

    def predict(self, frames) -> float:
        import numpy as np
        from PIL import Image

        if len(frames) < 2:
            raise ValueError("insufficient_frames")
        torch = self.torch
        scores = []
        # One frame at a time bounds activation memory on small CPU machines.
        for i in np.linspace(0, len(frames) - 1, self.frames).round().astype(int):
            rgb = np.ascontiguousarray(frames[i][:, :, ::-1])
            image = Image.fromarray(rgb).resize((224, 224), Image.Resampling.BILINEAR)
            array = np.array(image, dtype=np.float32) / 255
            tensor = torch.from_numpy(array).permute(2, 0, 1).sub_(0.5).div_(0.5).unsqueeze(0)
            with torch.inference_mode():
                logits = self.module(tensor.to(self.device))
            if tuple(logits.shape) != (1, 2) or not bool(torch.isfinite(logits).all()):
                raise ValueError("model_output_invalid")
            scores.append(float(torch.softmax(logits, dim=1)[0, 1].item()))
        return sum(scores) / len(scores)


class LocalVideoMAEAdapter:
    """Opt-in noncommercial offline demo adapter. Strict pinned safetensors only.

    Attribution: cliffer1 VideoMAE violence/nonviolence fine-tune and MCG-NJU
    VideoMAE base, CC BY-NC 4.0. Unknown training-data provenance; no field claim.
    """

    def __init__(self, path, settings):
        if not (
            settings.model_rights_confirmed
            and getattr(settings, "runtime_mode", None) in {"offline_demo", "online_demo"}
            and settings.environment == "evaluation"
            and settings.auth_mode == "local"
        ):
            raise ModelBlocked("videomae_noncommercial_demo_only")
        try:
            data = json.loads(path.read_text())
            if (
                data.get("format") != "videomae_safetensors_demo"
                or data.get("license") != "CC-BY-NC-4.0"
                or data.get("revision") != "613844b1d2d27f68944619bf91885a9befb101ac"
                or data.get("labels") != ["NonViolence", "Violence"]
                or data.get("frames") != 16
                or not all(data.get(k) for k in ("name", "source_url", "rights_record"))
            ):
                raise ValueError
            directory = (path.parent / data["checkpoint_dir"]).resolve()
            pinned = {
                "config.json": "59e7a7ba3196e7d7272304b16dc59e15b09b9f17832d76e8cbd9755487afa8ed",
                "preprocessor_config.json": "4bc9b255ff212041bc28127d4fa0ae1630abca52bb2d882f3e4c0b9e5b763f06",
                "README.md": "0d2adb5b283c3c4bd8fd087d5ce85d0c2a6528c7ca6cc6ff65da51645452614f",
                "model.safetensors": "5166b055ebee1439d6517c5bf2d516dce7a1f65afc53367f43b5848970853d53",
            }
            if data.get("files") != pinned:
                raise ValueError
            for filename, digest in pinned.items():
                sha = hashlib.sha256()
                with (directory / filename).open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        sha.update(chunk)
                if sha.hexdigest() != digest:
                    raise ModelBlocked("model_digest_mismatch")
            config = json.loads((directory / "config.json").read_text())
            if (
                config.get("id2label") != {"0": "NonViolence", "1": "Violence"}
                or config.get("num_frames") != 16
            ):
                raise ValueError
        except ModelBlocked:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise ModelBlocked("model_manifest_invalid") from None
        try:
            import torch
            from safetensors.torch import load_file
            from transformers import VideoMAEConfig, VideoMAEForVideoClassification, VideoMAEImageProcessor
        except ImportError:
            raise ModelBlocked("videomae_runtime_missing") from None
        self.torch, self.name, self.frames, self.device = (
            torch,
            data["name"] + "@" + data["revision"],
            16,
            "cpu",
        )
        torch.set_num_threads(2)
        try:
            self.module = VideoMAEForVideoClassification(VideoMAEConfig.from_dict(config))
            self.module.load_state_dict(load_file(str(directory / "model.safetensors")), strict=True)
            self.module.eval()
            self.processor = VideoMAEImageProcessor.from_dict(
                json.loads((directory / "preprocessor_config.json").read_text())
            )
        except Exception:
            raise ModelBlocked("model_checkpoint_incompatible") from None

    def predict(self, frames):
        import numpy as np

        if len(frames) < 2:
            raise ValueError("insufficient_frames")
        indices = np.linspace(0, len(frames) - 1, self.frames).round().astype(int)
        rgb = [np.ascontiguousarray(frames[i][:, :, ::-1]) for i in indices]
        inputs = self.processor(images=rgb, return_tensors="pt")
        if tuple(inputs["pixel_values"].shape) != (1, 16, 3, 224, 224):
            raise ValueError("model_input_shape_invalid")
        with self.torch.inference_mode():
            logits = self.module(**inputs).logits
        if tuple(logits.shape) != (1, 2) or not bool(self.torch.isfinite(logits).all()):
            raise ValueError("model_output_invalid")
        return float(self.torch.softmax(logits, dim=1)[0, 1].item())


def load_model(settings) -> ModelAdapter:
    path = getattr(settings, "model_manifest", None)
    if not path:
        raise ModelBlocked("model_manifest_missing")
    path = Path(path)
    try:
        if path.stat().st_size > 65536:
            raise ValueError("oversize manifest")
        data = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        raise ModelBlocked("model_manifest_invalid") from None
    if data.get("format") == "timm_vit_safetensors":
        return LocalViTAdapter(path, settings.model_rights_confirmed, settings.model_device)
    if data.get("format") == "videomae_safetensors_demo":
        return LocalVideoMAEAdapter(path, settings)
    manifest = ModelManifest.read(path, settings.model_rights_confirmed)
    return TorchScriptAdapter(manifest, settings.model_device)
