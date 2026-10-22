"""Disposable health-persistence faults; models/slow stores are explicit doubles."""

import logging
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dormguard.config import Settings
from dormguard.telemetry import HealthPublisher
from dormguard.worker import Worker, iso
from scripts.verify_rtsp import generate_fixture


class ControlledNegativeModelDouble:
    name = "CONTROLLED_NEGATIVE_TELEMETRY_TEST_DOUBLE"

    def __init__(self):
        self.predicted = threading.Event()

    def predict(self, frames):
        self.predicted.set()
        return 0.1


class BlockingHealthStoreDouble:
    """Only health SQL is blocked; no provider or real camera is contacted."""

    def __init__(self, camera):
        self.camera = camera
        self.entered, self.release = threading.Event(), threading.Event()
        self.health = []
        self.heartbeats = []

    def cameras(self, private=False):
        return [self.camera]

    def set_camera_health(self, camera_id, state, **fields):
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("CONTROLLED_HEALTH_SQL_TIMEOUT")
        self.health.append((camera_id, state, fields))

    def heartbeat(self, name, state, **fields):
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("CONTROLLED_HEALTH_SQL_TIMEOUT")
        self.heartbeats.append((name, state, fields))


class WorkerHealthTests(unittest.TestCase):
    def test_late_heartbeat_commit_does_not_make_old_observation_fresh(self):
        from dormguard.store import Store

        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            self.addCleanup(store.db.close)
            # Models a telemetry call which commits long after observation.
            store.heartbeat("inference", "ready", metrics={"observed_at_utc": iso(time.time() - 60)})
            self.assertTrue(store.health()[0]["stale"])
            store.heartbeat("inference", "ready", metrics={"observed_at_utc": iso(time.time())})
            self.assertFalse(store.health()[0]["stale"])

    def test_blocked_health_write_does_not_block_decoder_or_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = root / "test_sources"
            sources.mkdir()
            source = generate_fixture(sources / "generated.avi", seconds=2, fps=10)
            camera = {"id": "health-test", "kind": "test", "source_ref": "DORMGUARD_CAMERA_HEALTH_TEST"}
            store, model = BlockingHealthStoreDouble(camera), ControlledNegativeModelDouble()
            worker = Worker(Settings(root, window_seconds=1, window_stride=0.5), store, model)
            failures = []

            def run():
                try:
                    worker.run(once=True)
                except Exception as error:
                    failures.append(type(error).__name__)

            with patch.dict(os.environ, {camera["source_ref"]: str(source)}):
                thread = threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(store.entered.wait(2), "controlled health write was not reached")
                    self.assertTrue(model.predicted.wait(1.6), "health SQL stalled decoding/inference")
                    self.assertGreater(worker.states[camera["id"]].decoded_frames, 5)
                finally:
                    store.release.set()
                    thread.join(timeout=8)
                    worker.stop.set()
            self.assertFalse(thread.is_alive())
            self.assertFalse(failures)
            self.assertEqual(store.health[-1][1], "completed")
            self.assertEqual(store.heartbeats[-1][1], "stopped")
            self.assertFalse(worker.health_publisher.thread.is_alive())
            with self.assertRaisesRegex(RuntimeError, "camera_worker_cannot_restart"):
                worker.run(once=True)


class HealthPublisherTests(unittest.TestCase):
    def publisher(self, store, **options):
        publisher = HealthPublisher(store, **options)
        publisher.start()
        self.addCleanup(publisher.close)
        return publisher

    def test_rate_limit_coalesces_to_latest_state_and_drains_on_shutdown(self):
        store = BlockingHealthStoreDouble({})
        store.release.set()
        publisher = self.publisher(store, interval=10)
        publisher.submit("camera", "camera-a", state="connecting")
        self.assertTrue(store.entered.wait(1))
        for index in range(50):
            publisher.submit("camera", "camera-a", state="connected", last_frame_at=index)
        publisher.submit("camera", "camera-a", state="completed", last_frame_at=50)
        self.assertEqual(len(store.health), 1, "unchanged heartbeats were not rate-limited")
        self.assertTrue(publisher.close())
        self.assertEqual(len(store.health), 2)
        self.assertEqual(store.health[-1], ("camera-a", "completed", {"last_frame_at": 50}))
        self.assertEqual(publisher.snapshot()["coalesced"], 50)
        self.assertFalse(publisher.submit("camera", "camera-a", state="connected"))

    def test_bounded_pending_while_sql_is_blocked_and_fresh_observations_win(self):
        store = BlockingHealthStoreDouble({})
        publisher = self.publisher(store, capacity=2, interval=10)
        publisher.submit("camera", "in-flight", state="connected")
        self.assertTrue(store.entered.wait(1))
        try:
            for index in range(100):
                publisher.submit("camera", str(index), state="connected")
            publisher.submit("camera", "99", state="disconnected")
            metrics = publisher.snapshot()
            self.assertEqual(metrics["pending"], 2)
            self.assertEqual(metrics["in_flight"], 1)
            self.assertEqual(metrics["dropped"], 98)
        finally:
            store.release.set()
        self.assertTrue(publisher.close())
        self.assertEqual([row[0] for row in store.health], ["in-flight", "98", "99"])
        self.assertEqual(store.health[-1][1], "disconnected")

    def test_failed_sql_is_visible_redacted_not_replayed_and_recovers(self):
        class FaultStoreDouble(BlockingHealthStoreDouble):
            def set_camera_health(self, camera_id, state, **fields):
                self.entered.set()
                if not self.release.is_set():
                    raise OSError("rtsp://PRIVATE_USER:PRIVATE_SECRET@127.0.0.1/PRIVATE")
                self.health.append((camera_id, state, fields))

        store = FaultStoreDouble({})
        publisher = self.publisher(store, interval=0.02)
        with self.assertLogs("dormguard.telemetry", logging.WARNING) as captured:
            publisher.submit("camera", "camera-a", state="connected")
            deadline = time.monotonic() + 1
            while publisher.snapshot()["write_failures"] == 0 and time.monotonic() < deadline:
                time.sleep(0.001)
            # No fresh observation means no retry, even over several intervals.
            time.sleep(0.06)
        self.assertEqual(publisher.snapshot()["write_failures"], 1)
        self.assertEqual(publisher.snapshot()["last_error_type"], "OSError")
        self.assertNotIn("PRIVATE", "".join(captured.output))
        store.release.set()
        publisher.submit("camera", "camera-a", state="completed")
        self.assertTrue(publisher.close())
        self.assertEqual(store.health[-1][1], "completed")
        self.assertIsNone(publisher.snapshot()["last_error_type"])

    def test_shutdown_timeout_is_bounded_and_does_not_start_more_writes(self):
        store = BlockingHealthStoreDouble({})
        publisher = self.publisher(store)
        publisher.submit("camera", "in-flight", state="connected")
        self.assertTrue(store.entered.wait(1))
        publisher.submit("worker", "inference", state="stopped")
        started = time.monotonic()
        try:
            with self.assertLogs("dormguard.telemetry", logging.WARNING):
                self.assertFalse(publisher.close(timeout=0.02))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(publisher.snapshot()["shutdown_incomplete"])
            self.assertEqual(publisher.snapshot()["pending"], 0)
        finally:
            store.release.set()
            publisher.thread.join(timeout=1)
        self.assertFalse(publisher.thread.is_alive())
        self.assertFalse(store.heartbeats)

    def test_outdated_queued_readiness_is_dropped_instead_of_refreshed(self):
        store = BlockingHealthStoreDouble({})
        store.release.set()
        publisher = HealthPublisher(store)
        with patch("dormguard.telemetry.time.monotonic", return_value=0):
            publisher.submit("worker", "inference", state="ready")
        publisher.start()
        self.addCleanup(publisher.close)
        self.assertTrue(publisher.close())
        self.assertEqual(publisher.snapshot()["dropped"], 1)
        self.assertFalse(store.heartbeats)


if __name__ == "__main__":
    unittest.main()
