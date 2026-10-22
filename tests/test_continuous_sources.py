"""Finite-source EOF semantics: controlled capture plus generated no-human video."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dormguard.config import Settings
from dormguard.model import ModelBlocked
from dormguard.store import Store
from dormguard.worker import Worker


class ControlledEOFOnlyCapture:
    def __init__(self):
        self.released = False

    def isOpened(self):
        return True

    def get(self, property_id):
        return 4

    def read(self):
        return False, None

    def release(self):
        self.released = True


class ControlledHealthStore:
    def __init__(self):
        self.observations = []

    def set_camera_health(self, camera_id, **fields):
        self.observations.append(fields)


class ControlledStopAfterBackoff(threading.Event):
    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.set()
        return True


def controlled_model_unavailable(settings):
    raise ModelBlocked("CONTROLLED_NO_INFERENCE_MODEL")


class ContinuousSourceTests(unittest.TestCase):
    def test_continuous_eof_is_nonterminal_and_keeps_existing_restart_backoff(self):
        with tempfile.TemporaryDirectory(prefix="dormguard-continuous-eof-") as directory:
            store = ControlledHealthStore()
            worker = Worker(Settings(Path(directory)), store, model_loader=controlled_model_unavailable)
            camera = {"id": "controlled", "kind": "test"}
            state = worker._new_state(camera)
            capture = ControlledEOFOnlyCapture()
            local_stop = ControlledStopAfterBackoff()
            with patch("dormguard.worker.open_capture", return_value=capture):
                worker._decoder("CONTROLLED_EOF_CAPTURE", [state], local_stop, False, 30)
            self.assertEqual([row["state"] for row in store.observations], ["reconnecting"])
            self.assertEqual(store.observations[0]["detail"], "simulated_test_video_restarting")
            self.assertEqual(local_stop.waits, [1])
            self.assertTrue(capture.released)
            self.assertEqual(worker.errors, 0)
            self.assertEqual(worker.inferred, 0)

    def test_once_eof_remains_terminal_and_does_not_restart(self):
        with tempfile.TemporaryDirectory(prefix="dormguard-once-eof-") as directory:
            store = ControlledHealthStore()
            worker = Worker(Settings(Path(directory)), store, model_loader=controlled_model_unavailable)
            state = worker._new_state({"id": "controlled", "kind": "test"})
            capture = ControlledEOFOnlyCapture()
            local_stop = ControlledStopAfterBackoff()
            with patch("dormguard.worker.open_capture", return_value=capture):
                worker._decoder("CONTROLLED_EOF_CAPTURE", [state], local_stop, True, 30)
            self.assertEqual([row["state"] for row in store.observations], ["completed"])
            self.assertEqual(store.observations[0]["detail"], "simulated_test_video_ended")
            self.assertEqual(local_stop.waits, [])
            self.assertTrue(capture.released)

    def test_actual_generated_video_crosses_two_eofs_without_terminal_camera_health(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory(prefix="dormguard-generated-loop-") as directory:
            root = Path(directory)
            sources = root / "test_sources"
            sources.mkdir()
            source = sources / "generated.avi"
            encoder = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"MJPG"), 4, (32, 32))
            self.assertTrue(encoder.isOpened())
            try:
                for index in range(4):
                    encoder.write(np.full((32, 32, 3), index * 40, dtype=np.uint8))
            finally:
                encoder.release()
            store = Store(root)
            self.addCleanup(store.db.close)
            director = store.provision_user("controlled@example.invalid", "director")
            camera = store.create_camera("Generated", "Test", "test", "DORMGUARD_CAMERA_LOOP", director["id"])
            worker = Worker(
                Settings(root, auth_mode="local"), store, model_loader=controlled_model_unavailable
            )
            observations, failures = [], []
            second_eof = threading.Event()
            original_health = worker._health
            original_write = store.set_camera_health

            def observed_health(state, status, detail=""):
                observations.append((status, detail))
                if sum("simulated_test_video_" in item[1] for item in observations) >= 2:
                    second_eof.set()
                original_health(state, status, detail)

            def delayed_sql(*args, **kwargs):
                time.sleep(0.02)  # Explicit controlled delay before actual disposable SQL.
                original_write(*args, **kwargs)

            def run():
                try:
                    worker.run()
                except Exception as error:
                    failures.append(type(error).__name__)

            with (
                patch.dict(os.environ, {"DORMGUARD_CAMERA_LOOP": str(source)}),
                patch.object(worker, "_health", side_effect=observed_health),
                patch.object(store, "set_camera_health", side_effect=delayed_sql),
            ):
                thread = threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(second_eof.wait(6), "Actual generated source did not reach two EOFs")
                finally:
                    worker.stop.set()
                    thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertGreaterEqual(worker.states[camera["id"]].decoded_frames, 8)
            self.assertNotIn("completed", [status for status, _ in observations])
            self.assertEqual(store.camera(camera["id"])["state"], "reconnecting")
            frame = cv2.imread(str(root / "frames" / (camera["id"] + ".jpg")))
            self.assertEqual(frame.shape[:2], (32, 32))
            self.assertEqual(worker.inferred, 0)
            self.assertEqual(store.incidents(), [])


if __name__ == "__main__":
    unittest.main()
