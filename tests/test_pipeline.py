"""Disposable camera/pipeline regressions. Positive models here are test doubles.

These tests do not establish real-model violence detection or camera deployment
readiness. The generated camera video contains colored shapes and no people.
"""

import hashlib
import importlib.util
import json
import math
import os
import shutil
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dormguard.config import Settings
from dormguard.model import LocalViTAdapter, ModelBlocked, ModelManifest, load_model
from dormguard.worker import TemporalGate, Window, Worker

VIDEO_RUNTIME = bool(importlib.util.find_spec("cv2") and importlib.util.find_spec("numpy"))


class ControlledModelDouble:
    """Explicit deterministic positive test double; never imported by production."""

    name = "CONTROLLED_TEST_DOUBLE"
    frames = 4

    def predict(self, frames):
        return 0.9


class MemoryStoreDouble:
    """Interface double which records evidence bytes before worker temp cleanup."""

    def __init__(self, cameras=()):
        self.rows = list(cameras)
        self.health_rows, self.heartbeats, self.incidents, self.evidence = {}, [], [], []

    def cameras(self, private=False):
        return self.rows

    def set_camera_health(self, camera_id, state, last_frame_at=None, detail=""):
        self.health_rows[camera_id] = {"state": state, "last_frame_at": last_frame_at, "detail": detail}

    def heartbeat(self, name, state, detail="", metrics=None):
        self.heartbeats.append({"name": name, "state": state, "detail": detail, "metrics": metrics})

    def create_incident(self, camera_id, score, model, **kwargs):
        row = dict(kwargs, id="incident-test-1", camera_id=camera_id, score=score, model=model)
        row["evidence_bytes"] = Path(kwargs["evidence_source"]).read_bytes()
        self.incidents.append(row)
        return row

    def add_evidence(self, incident_id, source_path, **kwargs):
        self.evidence.append(dict(kwargs, incident_id=incident_id, data=Path(source_path).read_bytes()))


class TemporalTests(unittest.TestCase):
    def test_private_capture_disables_native_debug_before_decoder_import(self):
        import subprocess
        import sys

        environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}}
        environment.update(OPENCV_LOG_LEVEL="DEBUG", OPENCV_FFMPEG_LOGLEVEL="56")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; from dormguard.worker import open_capture; assert os.environ['OPENCV_LOG_LEVEL']=='SILENT'; c=open_capture('rtsp://controlled-user:controlled-secret@127.0.0.1:1/no-camera'); c.release()",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=12,
            check=True,
        )
        self.assertNotIn("controlled-secret", result.stdout + result.stderr)
        self.assertNotIn("controlled-user", result.stdout + result.stderr)

    def test_single_positive_cannot_trigger_and_two_of_three_can(self):
        gate = TemporalGate(0.7)
        self.assertFalse(gate.observe(0.9, 100, now=100))
        self.assertFalse(gate.observe(0.2, 101, now=101))
        self.assertTrue(gate.observe(0.8, 102, now=102))

    def test_cooldown_and_fresh_confirmation(self):
        gate = TemporalGate(0.7, cooldown=60, max_gap=100)
        gate.observe(0.9, 100, now=100)
        self.assertTrue(gate.observe(0.9, 101, now=101))
        self.assertFalse(gate.observe(0.9, 102, now=102))
        self.assertFalse(gate.observe(0.9, 103, now=103))
        self.assertTrue(gate.observe(0.9, 162, now=162))

    def test_gaps_do_not_accumulate_unrelated_positive_windows(self):
        gate = TemporalGate(0.7, max_gap=6)
        gate.observe(0.9, 100, now=100)
        self.assertFalse(gate.observe(0.9, 110, now=110))
        self.assertTrue(gate.observe(0.9, 111, now=111))

    def test_score_and_timestamp_validation(self):
        for score, timestamp, now in [
            (math.nan, 100, 100),
            (1.1, 100, 100),
            (0.9, math.inf, 100),
            (0.9, 104, 100),
            (0.9, 1, 200),
        ]:
            with self.subTest(score=score, timestamp=timestamp):
                with self.assertRaises(ValueError):
                    TemporalGate(0.7).observe(score, timestamp, now=now)
        gate = TemporalGate(0.7)
        gate.observe(0.9, 100, now=100)
        with self.assertRaises(ValueError):
            gate.observe(0.9, 100, now=100)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "checkpoint.pt").write_bytes(b"CONTROLLED NONMODEL TEST BYTES")
        self.data = {
            "name": "test",
            "format": "torchscript",
            "checkpoint": "checkpoint.pt",
            "sha256": hashlib.sha256((self.root / "checkpoint.pt").read_bytes()).hexdigest(),
            "frames": 16,
            "size": 224,
            "mean": [0.45] * 3,
            "std": [0.225] * 3,
            "labels": ["non_violence", "violence"],
            "suspected_index": 1,
            "source_url": "https://example.invalid/controlled-test-double",
            "license": "controlled test",
            "rights_record": "controlled test only",
            "input_layout": "NCTHW",
            "output": "logits",
            "color": "RGB",
        }
        self.path = self.root / "manifest.json"
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.data))

    def test_requires_rights_confirmation_before_loading(self):
        with self.assertRaisesRegex(ModelBlocked, "model_rights_unconfirmed"):
            ModelManifest.read(self.path)

    def test_pins_checkpoint_and_rejects_tamper(self):
        self.assertEqual(ModelManifest.read(self.path, True).frames, 16)
        (self.root / "checkpoint.pt").write_bytes(b"changed")
        with self.assertRaisesRegex(ModelBlocked, "model_digest_mismatch"):
            ModelManifest.read(self.path, True)

    def test_invalid_preprocessing_and_class_index_fail_closed(self):
        for key, value in [
            ("std", [0, 0, 0]),
            ("frames", 0),
            ("suspected_index", 4),
            ("labels", ["same", "same"]),
            ("color", "BGR"),
        ]:
            with self.subTest(key=key):
                old = self.data[key]
                self.data[key] = value
                self.write()
                with self.assertRaises(ModelBlocked):
                    ModelManifest.read(self.path, True)
                self.data[key] = old

    def test_no_configuration_never_returns_fake_detector(self):
        with self.assertRaisesRegex(ModelBlocked, "model_manifest_missing"):
            load_model(Settings(self.root))

    def test_vit_refuses_partial_checkpoint_instead_of_random_initialized_model(self):
        """Regression for actual HF metadata / timm weight-key discrepancy."""
        pre = {
            "size": {"height": 224, "width": 224},
            "image_mean": [0.5] * 3,
            "image_std": [0.5] * 3,
            "resample": 2,
            "do_normalize": True,
            "do_rescale": True,
            "do_resize": True,
            "rescale_factor": 1 / 255,
        }
        config = {"architectures": ["ViTForImageClassification"], "num_labels": 2, "image_size": 224}
        (self.root / "config.json").write_text(json.dumps(config))
        (self.root / "preprocessor_config.json").write_text(json.dumps(pre))
        (self.root / "model.safetensors").write_bytes(b"EXPLICIT CONTROLLED CHECKPOINT DOUBLE")
        files = {
            x: hashlib.sha256((self.root / x).read_bytes()).hexdigest()
            for x in ("config.json", "preprocessor_config.json", "model.safetensors")
        }
        manifest = dict(
            self.data,
            format="timm_vit_safetensors",
            architecture="vit_base_patch16_224",
            frames=4,
            aggregation="mean_frame_softmax",
            checkpoint_dir=".",
            revision="test",
            label_mapping_source="controlled test",
            files=files,
        )
        self.path.write_text(json.dumps(manifest))
        calls = []

        class ControlledIncompatibleModule:
            def load_state_dict(self, weights, strict=False):
                calls.append(strict)
                raise RuntimeError("controlled missing keys")

        fake_torch = SimpleNamespace(set_num_threads=lambda _: None)
        fake_timm = SimpleNamespace(create_model=lambda *a, **k: ControlledIncompatibleModule())
        fake_safetensors = SimpleNamespace(load_file=lambda *a, **k: {})
        with patch.dict(
            "sys.modules", {"torch": fake_torch, "timm": fake_timm, "safetensors.torch": fake_safetensors}
        ):
            with self.assertRaisesRegex(ModelBlocked, "model_checkpoint_incompatible"):
                LocalViTAdapter(self.path, True, "cpu")
        self.assertEqual(calls, [True])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            Path(self.temp.name), window_seconds=1, window_stride=0.5, inference_fps=4, max_inference_queue=2
        )
        self.store = MemoryStoreDouble()
        self.worker = Worker(self.settings, self.store, ControlledModelDouble())
        self.state = self.worker._new_state({"id": "camera-test", "kind": "test"})
        self.worker.states["camera-test"] = self.state

    def test_queue_has_backpressure_and_keeps_recent_window(self):
        for index in range(20):
            self.worker._enqueue(Window(self.state, (), float(index)))
        # Latest-only per camera admission avoids consuming every bounded slot
        # with stale windows from a single fast producer.
        self.assertEqual(self.worker.queue.qsize(), 1)
        self.assertEqual(self.worker.dropped, 19)
        self.assertEqual(self.worker.queue.get().timestamp, 19)

    def test_real_model_unavailable_is_explicit_and_capture_initializes(self):
        worker = Worker(self.settings, self.store)
        worker.heartbeat()
        self.assertIsNone(worker.model)
        self.assertEqual(self.store.heartbeats[-1]["state"], "blocked")
        self.assertEqual(self.store.heartbeats[-1]["detail"], "model_manifest_missing")
        self.assertTrue(worker.frames_dir.is_dir())

    def test_over_budget_drops_inference_without_inventing_success(self):
        now = time.time()
        with patch("dormguard.worker.rss_mb", return_value=99999):
            self.worker.process(Window(self.state, ((now - 1, object()), (now, object())), now))
        self.assertEqual(self.worker.inferred, 0)
        self.assertEqual(self.worker.dropped, 1)
        self.assertEqual(self.worker.model_detail, "rss_budget_inference_paused")

    def test_stale_window_dropped(self):
        self.worker.process(Window(self.state, (), time.time() - 60))
        self.assertEqual(self.worker.dropped, 1)
        self.assertFalse(self.store.incidents)

    def test_disabled_camera_queued_window_is_dropped(self):
        self.worker.active_ids = set()
        self.worker.process(Window(self.state, (), time.time()))
        self.assertEqual(self.worker.dropped, 1)
        self.assertFalse(self.store.incidents)

    def test_private_runtime_directory_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as private:
            root = Path(private)
            (root / "frames").symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "frames_directory_symlink_refused"):
                Worker(Settings(root), MemoryStoreDouble())

    def test_controlled_positive_persists_only_after_confirmation_and_real_boundaries(self):
        now = time.time() - 1
        self.state.ring = deque([(now - 1, object()), (now - 0.5, object()), (now, object())])
        encoded = self.settings.data_dir / "CONTROLLED-ENCODER-DOUBLE.mp4"

        def controlled_encode(frames):
            encoded.write_bytes(b"EXPLICIT CONTROLLED ENCODER DOUBLE; NOT REAL EVIDENCE")
            return encoded

        with patch.object(self.worker, "_encode", side_effect=controlled_encode):
            self.worker.process(Window(self.state, tuple(self.state.ring), now - 0.5))
            self.assertFalse(self.store.incidents)
            self.worker.process(Window(self.state, tuple(self.state.ring), now))
        self.assertEqual(len(self.store.incidents), 1)
        row = self.store.incidents[0]
        self.assertLess(row["clip_start"], row["clip_end"])
        self.assertEqual(row["model"], "CONTROLLED_TEST_DOUBLE")
        self.assertFalse(encoded.exists())
        self.assertEqual(self.state.pending_incident, "incident-test-1")
        self.worker.flush_post(final=True)
        self.assertFalse(self.store.evidence)
        self.assertEqual(self.store.health_rows["camera-test"]["detail"], "post_evidence_unavailable")

    def test_encoder_failure_does_not_create_fake_incident(self):
        now = time.time() - 1
        self.state.ring = deque([(now - 1, object()), (now, object())])
        self.worker.process(Window(self.state, tuple(self.state.ring), now - 0.5))
        with patch.object(self.worker, "_encode", side_effect=RuntimeError("controlled codec failure")):
            with self.assertRaises(RuntimeError):
                self.worker.process(Window(self.state, tuple(self.state.ring), now))
        self.assertFalse(self.store.incidents)


@unittest.skipUnless(VIDEO_RUNTIME, "OpenCV/numpy are required for genuine decoder checks")
class ActualTransportTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for real evidence encoding")
    def test_real_video_store_evidence_outbox_with_controlled_positive_model(self):
        """Actual decoder/SQL/H264/hash; only prediction is a labeled double."""
        from dormguard.store import Store
        from scripts.verify_rtsp import generate_fixture

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_dir = root / "test_sources"
            fixture_dir.mkdir()
            video = generate_fixture(fixture_dir / "generated.avi", seconds=3, fps=10)
            store = Store(root).init()
            director = store.provision_user("director@example.invalid", "director")
            store.provision_user("deputy@example.invalid", "deputy_director")
            store.create_camera(
                "Generated test camera", "No humans", "test", "DORMGUARD_CAMERA_PIPELINE", director["id"]
            )
            settings = Settings(
                root,
                window_seconds=1,
                window_stride=0.5,
                inference_fps=10,
                evidence_pre_seconds=1,
                evidence_post_seconds=0.5,
            )
            worker = Worker(settings, store, ControlledModelDouble())
            with patch.dict(os.environ, {"DORMGUARD_CAMERA_PIPELINE": str(video)}):
                worker.run(once=True)
            incidents = store.incidents()
            self.assertEqual(len(incidents), 1)
            incident = store.incident(incidents[0]["id"])
            self.assertEqual(incident["status"], "DETECTED")
            self.assertEqual(incident["model"], "CONTROLLED_TEST_DOUBLE")
            self.assertGreaterEqual(len(incident["evidence"]), 2)
            for item in incident["evidence"]:
                private = store.evidence(item["id"], private=True)
                evidence = store.evidence_path(private)
                self.assertEqual(hashlib.sha256(evidence.read_bytes()).hexdigest(), item["sha256"])
                self.assertLess(item["clip_start"], item["clip_end"])
            with store.db.read() as conn:
                count = conn.execute("SELECT COUNT(*) AS n FROM notification_outbox").fetchone()["n"]
            self.assertEqual(count, 2)
            self.assertFalse(list((root / "capture").glob("*.mp4")))

    def test_real_generated_video_shared_decoder_and_atomic_preview_with_model_blocked(self):
        from scripts.verify_rtsp import generate_fixture

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_dir = root / "test_sources"
            fixture_dir.mkdir()
            video = generate_fixture(fixture_dir / "generated.avi", seconds=0.8)
            cameras = [
                {"id": "camera-a", "kind": "test", "enabled": True, "source_ref": "DORMGUARD_CAMERA_TEST_A"},
                {"id": "camera-b", "kind": "test", "enabled": True, "source_ref": "DORMGUARD_CAMERA_TEST_B"},
            ]
            store = MemoryStoreDouble(cameras)
            worker = Worker(Settings(root), store)
            with patch.dict(
                os.environ, {"DORMGUARD_CAMERA_TEST_A": str(video), "DORMGUARD_CAMERA_TEST_B": str(video)}
            ):
                worker.run(once=True)
            self.assertEqual(len(worker.sources), 1)
            for camera in cameras:
                self.assertGreater((root / "frames" / (camera["id"] + ".jpg")).stat().st_size, 100)
                self.assertEqual(store.health_rows[camera["id"]]["state"], "completed")
            self.assertFalse(store.incidents)
            self.assertFalse(list((root / "frames").glob("*.tmp")))
            self.assertEqual(worker.inferred, 0)

    def test_timestamp_validation_before_frame_publication(self):
        import numpy as np

        with tempfile.TemporaryDirectory() as temporary:
            worker = Worker(Settings(Path(temporary)), MemoryStoreDouble())
            state = worker._new_state({"id": "camera-test", "kind": "test"})
            with self.assertRaises(ValueError):
                worker.ingest(state, np.zeros((10, 10, 3), dtype=np.uint8), time.time() + 30)
            self.assertFalse(list(worker.frames_dir.glob("*.jpg")))


if __name__ == "__main__":
    unittest.main()
