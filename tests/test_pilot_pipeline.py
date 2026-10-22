"""Fault injection on disposable real evidence/SQL; model is an explicit double."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from dormguard.config import Settings
from dormguard.store import Store
from dormguard.worker import FrameSampler, Window, Worker


class ControlledPositiveDouble:
    name = "CONTROLLED_POSITIVE_TEST_DOUBLE"

    def predict(self, frames):
        return 0.9


class PilotPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dormguard-pilot-pipeline-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name), max_rss_mb=65536)
        self.store = Store(self.settings.data_dir)
        director = self.store.provision_user("director@example.invalid", "director")
        self.store.provision_user("deputy@example.invalid", "deputy_director")
        camera = self.store.create_camera("Generated", "Lab", "test", "TEST", director["id"])
        self.worker = Worker(self.settings, self.store, ControlledPositiveDouble())
        self.state = self.worker._new_state(camera)
        now = time.time()
        frame = np.zeros((32, 48, 3), dtype=np.uint8)
        self.state.ring.extend([(now - 1, frame), (now - 0.3, frame)])
        self.first = now - 0.2
        self.worker.process(Window(self.state, tuple(self.state.ring), self.first))

    def test_failed_evidence_does_not_consume_cooldown_and_recovery_creates_incident(self):
        with patch.object(self.worker, "_encode", side_effect=OSError("CONTROLLED disk full")):
            with self.assertRaises(OSError):
                self.worker.process(Window(self.state, tuple(self.state.ring), self.first + 0.1))
        self.assertEqual(len(self.store.incidents()), 0)
        self.worker.process(Window(self.state, tuple(self.state.ring), self.first + 0.2))
        self.assertEqual(
            len(self.store.incidents()), 1, "Retry after transient storage failure must not wait 60 seconds"
        )

    def test_database_failure_rolls_back_temporal_commit_without_orphaned_copy(self):
        with patch.object(self.store, "create_incident", side_effect=OSError("CONTROLLED database outage")):
            with self.assertRaises(OSError):
                self.worker.process(Window(self.state, tuple(self.state.ring), self.first + 0.1))
        self.worker.process(Window(self.state, tuple(self.state.ring), self.first + 0.2))
        self.assertEqual(len(self.store.incidents()), 1)
        self.assertEqual(list(self.worker.temp_dir.iterdir()), [])

    def test_queue_admission_prioritizes_cameras_that_missed_previous_round(self):
        # Two deterministic saturated rounds. Simulate completed analysis after
        # dequeue; inference scores are irrelevant to scheduler fairness.
        states = [self.worker._new_state({"id": str(i)}) for i in range(8)]
        for round_number in range(2):
            for state in states:
                self.worker._enqueue(Window(state, (), time.time() + round_number))
            while not self.worker.queue.empty():
                self.worker.queue.get_nowait().state.analyzed_windows += 1
        self.assertTrue(
            all(state.analyzed_windows == 1 for state in states), [s.analyzed_windows for s in states]
        )

    def test_failed_persistence_does_not_restore_observations_across_disconnect(self):
        def interrupted_encode(frames):
            self.state.gate.reset()
            raise OSError("CONTROLLED disconnect during encode")

        with patch.object(self.worker, "_encode", side_effect=interrupted_encode):
            with self.assertRaises(OSError):
                self.worker.process(Window(self.state, tuple(self.state.ring), self.first + 0.1))
        self.worker.process(Window(self.state, tuple(self.state.ring), self.first + 0.2))
        self.assertEqual(len(self.store.incidents()), 0)

    def test_persisted_incident_has_timings_and_real_temporal_scores(self):
        self.worker.process(Window(self.state, tuple(self.state.ring), self.first + 0.1))
        incident = self.store.incident(self.store.incidents()[0]["id"])
        detected = next(e for e in incident["events"] if e["kind"] == "detected")["detail"]
        self.assertEqual(detected["correlation_id"], incident["id"])
        self.assertIsNone(detected["pipeline"]["T0"])
        self.assertEqual(len(detected["pipeline"]["temporal_decisions"]), 2)
        self.assertTrue(any(e["kind"] == "pipeline.persisted" for e in incident["events"]))

    def test_sampling_four_fps_from_ten_fps_does_not_drift_to_three_point_three(self):
        sampler = FrameSampler(4, 0)
        accepted = [i / 10 for i in range(200) if sampler.accept(i / 10)]
        self.assertEqual(len(accepted), 80)
        self.assertTrue(all(0.19 <= b - a <= 0.31 for a, b in zip(accepted, accepted[1:])))
        self.assertTrue(sampler.accept(100))
        self.assertFalse(sampler.accept(100))

    def test_disconnect_during_inference_discards_old_score_and_queued_window(self):
        pending = Window(self.state, tuple(self.state.ring), self.first + 0.2)

        def disconnect(frames):
            self.state.gate.reset()
            return 0.9  # Explicit model double; score belongs to pre-gap frames.

        with patch.object(self.worker.model, "predict", side_effect=disconnect):
            self.worker.process(Window(self.state, tuple(self.state.ring), self.first + 0.1))
        self.worker.process(pending)
        self.assertEqual(
            self.store.incidents(), [], "Pre-disconnect scores must not confirm after a continuity reset"
        )


if __name__ == "__main__":
    unittest.main()
