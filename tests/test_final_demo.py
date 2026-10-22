"""Disposable actual fixture/media/SQL demo checks. No model or provider success is simulated."""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from dormguard.app import create_app
from dormguard.config import Settings
from scripts.run_demo import FIXTURE_LABEL, prepare_demo


class DemoTests(unittest.TestCase):
    def test_fixture_is_labeled_private_reviewable_and_reset_is_isolated(self):
        with tempfile.TemporaryDirectory(prefix="dormguard-demo-parent-") as parent:
            unrelated = Path(parent) / "unrelated.txt"
            unrelated.write_text("Preserve unrelated data")
            with tempfile.TemporaryDirectory(dir=parent) as directory:
                settings, store, source, access_file = prepare_demo(directory, "fixture", 8877)
                access = json.loads(access_file.read_text())
                self.assertEqual(access_file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(settings.demo_mode, "fixture")
                self.assertIsNone(settings.model_manifest)
                self.assertFalse(settings.telegram_bot_token)
                self.assertEqual(store.incidents()[0]["model"], FIXTURE_LABEL)
                self.assertTrue(source.is_file())
                with TestClient(create_app(settings, store)) as client:
                    self.assertEqual(client.get("/api/v1/auth/config").json()["demo_mode"], "fixture")
                    self.assertEqual(client.get("/api/v1/incidents").status_code, 401)
                    login = client.post(
                        "/api/v1/auth/login", json={"email": access["email"], "password": access["password"]}
                    )
                    self.assertEqual(login.status_code, 200)
                    headers = {"X-CSRF-Token": login.json()["csrf_token"]}
                    incident = client.get("/api/v1/incidents/" + access["incident_id"]).json()
                    evidence = incident["evidence"][0]["id"]
                    self.assertTrue(
                        client.post(f"/api/v1/evidence/{evidence}/verify", headers=headers).json()["valid"]
                    )
                    self.assertEqual(client.get(f"/api/v1/evidence/{evidence}/video").status_code, 200)
                    self.assertEqual(
                        client.post(
                            f"/api/v1/incidents/{incident['id']}/acknowledge", headers=headers, json={}
                        ).status_code,
                        200,
                    )
                    self.assertTrue(store.verify_audit()["valid"])
                with self.assertRaisesRegex(ValueError, "existing data is never reset"):
                    prepare_demo(directory, "fixture", 8877)
            self.assertFalse(Path(directory).exists())
            self.assertEqual(unrelated.read_text(), "Preserve unrelated data")

    def test_inference_requires_real_config_and_demo_cannot_enable_external_delivery(self):
        with tempfile.TemporaryDirectory(prefix="dormguard-demo-guard-") as directory:
            with self.assertRaisesRegex(ValueError, "Inference requires"):
                prepare_demo(directory, "inference", 8877)
            self.assertEqual(list(Path(directory).iterdir()), [])
            with self.assertRaises(ValueError):
                Settings(Path(directory), auth_mode="supabase", demo_mode="fixture")
            with self.assertRaises(ValueError):
                Settings(
                    Path(directory),
                    auth_mode="local",
                    demo_mode="fixture",
                    telegram_bot_token="CONTROLLED_INVALID_TOKEN",
                )
