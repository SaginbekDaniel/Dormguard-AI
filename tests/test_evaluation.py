"""Evaluation math/annotation regressions with explicitly controlled model scores."""

import json
import tempfile
import unittest
from pathlib import Path

from dormguard.evaluation import decisions, distribution, evaluate_dataset, load_dataset, sha256, summarize
from scripts.verify_rtsp import generate_fixture

CONFIG = {
    "sample_fps": 4,
    "window_seconds": 1,
    "stride_seconds": 0.5,
    "confirmations": 2,
    "cooldown_seconds": 60,
}


class EvaluationTests(unittest.TestCase):
    def test_confusion_event_miss_and_camera_hours_use_duration_not_window_sum(self):
        sample = {
            "id": "s",
            "cohort": "proxy_behavior",
            "camera_id": "c",
            "scene_id": "scene",
            "scenario": "partial_occlusion",
            "start_seconds": 0,
            "end_seconds": 3600,
            "events": [{"start_seconds": 1, "end_seconds": 3}],
        }
        predictions = {
            "windows": [
                {"start_seconds": t - 0.5, "end_seconds": t, "score": score, "inference_seconds": 0.01}
                for t, score in [(1, 0.9), (2, 0.8), (3, 0.2), (100, 0.9), (101, 0.9), (102, 0.1)]
            ]
        }
        result = decisions(sample, predictions, 0.7, CONFIG)
        metrics = summarize([result])
        self.assertEqual(metrics["confusion"], {"tp": 2, "fn": 1, "fp": 2, "tn": 1})
        self.assertEqual(metrics["event_recall"], 1)
        self.assertEqual(metrics["false_alarms_per_camera_hour"], 1)
        self.assertEqual(result["events"][0]["time_to_confirmed_incident_seconds"], 1)
        self.assertEqual(metrics["evaluated_camera_hours"], 1)
        self.assertIsNone(distribution([1, 2])["p95"])
        missed = summarize([decisions(sample, predictions, 0.95, CONFIG)])
        self.assertEqual(missed["event_recall"], 0)
        self.assertIsNone(missed["precision"])
        self.assertEqual(missed["time_to_confirmation_seconds"]["count"], 0)

    def test_real_decoder_manifest_and_separate_cohorts_with_controlled_model(self):
        class ExplicitNegativeDouble:
            name = "CONTROLLED_NEGATIVE_TEST_DOUBLE"

            def predict(self, frames):
                return 0.1

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = generate_fixture(root / "generated.avi", seconds=4)
            sample = {
                "id": "a",
                "file": source.name,
                "sha256": sha256(source),
                "camera_id": "c",
                "scene_id": "generated",
                "scenario": "generated_shapes",
                "cohort": "infrastructure_smoke",
                "provenance": "Generated non-human fixture",
                "authorization": "Test-only generated fixture",
                "start_seconds": 0,
                "end_seconds": 2,
                "events": [],
            }
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "samples": [
                            sample,
                            {
                                **sample,
                                "id": "b",
                                "start_seconds": 2,
                                "end_seconds": 4,
                                "cohort": "proxy_behavior",
                            },
                        ],
                    }
                )
            )
            result = evaluate_dataset(manifest, root, ExplicitNegativeDouble(), CONFIG, [0.6, 0.7])
            self.assertEqual(len(result["sweeps"]), 2)
            cohorts = result["sweeps"][0]["cohorts"]
            self.assertEqual(len(cohorts), 2)
            self.assertNotIn("metrics", result)
            self.assertEqual(cohorts["infrastructure_smoke"]["metrics"]["false_alarms"], 0)
            self.assertEqual(cohorts["proxy_behavior"]["metrics"]["annotated_events"], 0)
            self.assertIsNone(cohorts["proxy_behavior"]["metrics"]["event_recall"])
            self.assertEqual(result["dataset_sha256"], sha256(manifest))
            sample["sha256"] = "0" * 64
            manifest.write_text(json.dumps({"version": 1, "samples": [sample]}))
            with self.assertRaisesRegex(ValueError, "digest"):
                load_dataset(manifest, root)

    def test_rejects_overlapping_coverage_and_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.bin"
            source.write_bytes(b"CONTROLLED_NONVIDEO_FIXTURE")
            sample = {
                "id": "a",
                "file": source.name,
                "sha256": sha256(source),
                "camera_id": "c",
                "scene_id": "s",
                "scenario": "s",
                "cohort": "infrastructure_smoke",
                "provenance": "generated",
                "authorization": "test",
                "start_seconds": 0,
                "end_seconds": 10,
                "events": [],
            }
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 1, "samples": [sample, {**sample, "id": "b"}]}))
            with self.assertRaisesRegex(ValueError, "overlapping_recording"):
                load_dataset(manifest, root)
            manifest.write_text(json.dumps({"version": 1, "samples": [{**sample, "file": "../missing"}]}))
            with self.assertRaisesRegex(ValueError, "outside_root"):
                load_dataset(manifest, root)
