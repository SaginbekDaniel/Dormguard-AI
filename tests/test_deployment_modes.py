"""Mode/readiness/authentication checks against actual disposable SQLite."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from dormguard.app import create_app
from dormguard.config import Settings
from dormguard.store import Store


class DeploymentModeTests(unittest.TestCase):
    def test_online_configuration_cannot_enable_offline_entry_or_insecure_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            kwargs = dict(
                data_dir=Path(directory),
                runtime_mode="online_demo",
                auth_mode="local",
                https=True,
                public_url="https://demo.example.invalid",
            )
            settings = Settings(**kwargs)
            self.assertEqual(settings.runtime_mode, "online_demo")
            for changes in (
                {"demo_mode": "offline"},
                {"demo_mode": "fixture"},
                {"https": False},
                {"public_url": "http://127.0.0.1:8000"},
                {"environment": "production"},
            ):
                with self.subTest(changes=changes):
                    with self.assertRaises(ValueError):
                        replace(settings, **changes)
            store = Store(Path(directory))
            try:
                with TestClient(create_app(settings, store), base_url=settings.public_url) as client:
                    self.assertEqual(
                        client.post(
                            "/api/v1/demo/enter", json={}, headers={"Origin": settings.public_url}
                        ).status_code,
                        404,
                    )
                    self.assertEqual(client.get("/api/v1/incidents").status_code, 401)
                    self.assertEqual(client.get("/api/v1/evidence/not-owned/video").status_code, 401)
                    self.assertEqual(client.get("/api/v1/cameras").status_code, 401)
            finally:
                store.db.close()

    def test_readiness_requires_fresh_real_worker_metadata_and_reports_source_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory), auth_mode="local")
            store = Store(Path(directory))
            try:
                with TestClient(create_app(settings, store)) as client:
                    self.assertEqual(client.get("/health").status_code, 200)
                    self.assertEqual(client.get("/ready").status_code, 503)
                    store.heartbeat(
                        "inference",
                        state="blocked",
                        detail="Controlled missing-model health metadata",
                        metrics={},
                    )
                    self.assertEqual(client.get("/ready").status_code, 503)
                    # Explicit health metadata fixture only. This test makes no model-execution claim.
                    store.heartbeat(
                        "inference",
                        state="ready",
                        detail="Controlled health metadata",
                        metrics={"model": "CONTROLLED_READY_METADATA"},
                    )
                    ready = client.get("/ready")
                    self.assertEqual(ready.status_code, 200)
                    self.assertFalse(ready.json()["video_source_active"])
                    store.heartbeat(
                        "inference",
                        state="stopped",
                        detail="",
                        metrics={"model": "CONTROLLED_READY_METADATA"},
                    )
                    self.assertEqual(client.get("/ready").status_code, 503)
            finally:
                store.db.close()
