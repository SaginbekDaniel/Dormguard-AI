"""Readiness regression: a listening API must not imply a functioning AI demo."""

import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.offline_demo import wait_for_ready


class OfflineStartupTests(unittest.TestCase):
    def test_alive_or_stale_worker_does_not_pass_before_real_readiness(self):
        observations = [
            {"status": "alive"},
            {"model_ready": True, "worker_fresh": False},
            {"model_ready": True, "worker_fresh": True},
        ]

        def reply(url, timeout):
            self.assertEqual(url, "http://127.0.0.1:8877/ready")
            response = io.StringIO(json.dumps(observations.pop(0)))
            response.status = 200
            return response

        with (
            patch("scripts.offline_demo.urlopen", side_effect=reply),
            patch("scripts.offline_demo.time.sleep"),
        ):
            wait_for_ready("http://127.0.0.1:8877", [], Path("private-run"))
        self.assertEqual(observations, [])

    def test_child_exit_and_readiness_timeout_fail_instead_of_printing_ready(self):
        process = SimpleNamespace(poll=lambda: 1)
        with self.assertRaisesRegex(RuntimeError, "exited during startup"):
            wait_for_ready("http://127.0.0.1:8877", [process], Path("private-run"))
        with patch("scripts.offline_demo.urlopen", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(RuntimeError, "readiness timed out"):
                wait_for_ready("http://127.0.0.1:8877", [], Path("private-run"), timeout=0)
