"""Real disposable SQLite/session/reset checks; no inference or provider test doubles."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from dormguard.app import create_app
from dormguard.config import Settings
from dormguard.offline import MARKER, prepare_generation
from dormguard.store import DomainError, Store


class OfflineDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dormguard-offline-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = prepare_generation(self.root, [])
        self.store = Store(self.directory)
        self.addCleanup(self.store.db.close)
        self.settings = Settings(
            self.directory, auth_mode="local", demo_mode="offline", public_url="http://127.0.0.1:8877"
        )
        self.client = TestClient(
            create_app(self.settings, self.store),
            base_url=self.settings.public_url,
            client=("127.0.0.1", 50000),
        )
        self.addCleanup(self.client.close)

    def enter(self):
        response = self.client.post(
            "/api/v1/demo/enter", json={}, headers={"Origin": self.settings.public_url}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": response.json()["csrf_token"]}

    def test_demo_entry_is_explicit_origin_bound_and_ordinary_session_logout_revokes(self):
        self.assertEqual(self.client.get("/api/v1/overview").status_code, 401)
        self.assertEqual(self.client.post("/api/v1/demo/enter", json={}).status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/v1/demo/enter", headers={"Origin": "https://attacker.invalid"}, json={}
            ).status_code,
            403,
        )
        headers = self.enter()
        self.assertEqual(self.client.get("/api/v1/auth/me").json()["role"], "director")
        self.assertEqual(self.client.get("/api/v1/overview").status_code, 200)
        cookie = self.client.cookies.get("dormguard_session")
        self.assertEqual(self.client.post("/api/v1/auth/logout", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/auth/logout", headers=headers, json={}).status_code, 200)
        self.assertEqual(
            self.client.get(
                "/api/v1/overview", headers={"Cookie": "dormguard_session=" + cookie}
            ).status_code,
            401,
        )
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_no_production_remote_provider_unmarked_workspace_or_remote_client(self):
        for changes in (
            {"auth_mode": "supabase"},
            {"environment": "production"},
            {"database_url": "postgresql://invalid"},
            {"public_url": "http://192.0.2.1:8877"},
            {"supabase_url": "https://example.invalid"},
            {"telegram_bot_token": "invalid"},
            {"camera_networks": "127.0.0.0/8"},
        ):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValueError):
                    replace(self.settings, **changes)
        client = TestClient(
            create_app(self.settings, self.store),
            base_url=self.settings.public_url,
            client=("192.0.2.1", 50000),
        )
        with client:
            self.assertEqual(client.get("/api/v1/auth/config").status_code, 403)
        self.assertEqual(
            self.client.get("/api/v1/auth/config", headers={"Host": "attacker.invalid"}).status_code, 403
        )
        (self.directory / MARKER).unlink()
        with self.assertRaises(DomainError):
            create_app(self.settings, self.store)

    def test_ordinary_local_mode_has_no_demo_access(self):
        client = TestClient(
            create_app(replace(self.settings, demo_mode=None, runtime_mode="evaluation"), self.store),
            base_url=self.settings.public_url,
        )
        with client:
            self.assertEqual(
                client.post(
                    "/api/v1/demo/enter", json={}, headers={"Origin": self.settings.public_url}
                ).status_code,
                404,
            )
            self.assertEqual(client.post("/api/v1/demo/reset", json={"confirm": True}).status_code, 404)

    def test_alerts_are_scoped_read_state_persists_and_telegram_is_independent(self):
        headers = self.enter()
        director = self.store.list_users()[0]
        camera = self.store.create_camera(
            "Local clip", "Demo", "test", "DORMGUARD_CAMERA_TEST", director["id"]
        )
        incident = self.store.create_incident(
            camera["id"], 0.8, "Controlled unit-test incident, no inference claim"
        )
        alert = self.client.get("/api/v1/alerts").json()
        self.assertEqual(alert["unread"], 1)
        self.assertEqual(alert["items"][0]["incident_id"], incident["id"])
        alert_id = alert["items"][0]["id"]
        self.assertEqual(self.client.post("/api/v1/alerts/" + alert_id + "/read", json={}).status_code, 403)
        for _ in range(2):
            self.assertEqual(
                self.client.post(
                    "/api/v1/alerts/" + alert_id + "/read", headers=headers, json={}
                ).status_code,
                200,
            )
        self.assertEqual(self.client.get("/api/v1/alerts").json()["unread"], 0)
        self.assertEqual(self.client.get("/api/v1/notifications").json()["items"][0]["state"], "queued")
        self.assertEqual(
            self.client.post("/api/v1/alerts/not-owned/read", headers=headers, json={}).status_code, 404
        )
        with self.store.db.read() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM audit_logs WHERE action='notification.read'"
                ).fetchone()["n"],
                1,
            )
        reopened = Store(self.directory)
        try:
            with TestClient(
                create_app(self.settings, reopened),
                base_url=self.settings.public_url,
                client=("127.0.0.1", 50001),
            ) as client:
                client.cookies.set("dormguard_session", self.client.cookies.get("dormguard_session"))
                self.assertEqual(client.get("/api/v1/alerts").json()["unread"], 0)
        finally:
            reopened.db.close()

    def test_reset_requires_csrf_confirmation_and_preserves_old_data(self):
        headers = self.enter()
        original = self.directory / "unrelated-original.txt"
        original.write_text("preserve")
        self.assertEqual(self.client.post("/api/v1/demo/reset", json={"confirm": True}).status_code, 403)
        self.assertEqual(
            self.client.post("/api/v1/demo/reset", headers=headers, json={"confirm": False}).status_code, 422
        )
        self.assertFalse((self.directory / "reset-request.json").exists())
        self.assertEqual(
            self.client.post("/api/v1/demo/reset", headers=headers, json={"confirm": True}).status_code, 200
        )
        self.assertTrue(json.loads((self.directory / "reset-request.json").read_text())["confirmed"])
        fresh = prepare_generation(self.root, [])
        self.assertNotEqual(fresh, self.directory)
        self.assertEqual(original.read_text(), "preserve")
        self.assertTrue((self.directory / "dormguard.sqlite3").exists())
        self.assertTrue(self.store.verify_audit()["valid"])


class OfflineModelBoundaryTests(unittest.TestCase):
    def test_videomae_cannot_load_outside_explicit_noncommercial_offline_scope(self):
        from dormguard.model import ModelBlocked, load_model

        manifest = Path(__file__).resolve().parents[1] / "docs/ai/OFFLINE_VIDEOMAE_MANIFEST.json"
        with tempfile.TemporaryDirectory() as directory:
            for changes in (
                {},
                {"demo_mode": "fixture"},
                {"model_rights_confirmed": False, "demo_mode": "offline"},
            ):
                settings = Settings(
                    Path(directory),
                    auth_mode="local",
                    model_manifest=manifest,
                    **({"model_rights_confirmed": True} | changes),
                )
                with self.assertRaisesRegex(ModelBlocked, "demo_only"):
                    load_model(settings)

    def test_offline_finite_eof_does_not_automatically_replay(self):
        from unittest.mock import patch

        from test_continuous_sources import (
            ControlledEOFOnlyCapture,
            ControlledHealthStore,
            ControlledStopAfterBackoff,
        )

        from dormguard.model import ModelBlocked
        from dormguard.worker import Worker

        with tempfile.TemporaryDirectory() as directory:
            store = ControlledHealthStore()

            def unavailable(settings):
                raise ModelBlocked("CONTROLLED_NO_MODEL_FOR_EOF_TEST")

            worker = Worker(
                Settings(Path(directory), auth_mode="local", demo_mode="offline"),
                store,
                model_loader=unavailable,
            )
            state = worker._new_state({"id": "controlled-offline-eof", "kind": "test"})
            capture = ControlledEOFOnlyCapture()
            stop = ControlledStopAfterBackoff()
            with patch("dormguard.worker.open_capture", return_value=capture):
                worker._decoder("CONTROLLED_EOF_CAPTURE", [state], stop, False, 30)
            self.assertEqual([r["state"] for r in store.observations], ["completed"])
            self.assertEqual(stop.waits, [])
            self.assertTrue(capture.released)


class OfflineResetSettingsTests(unittest.TestCase):
    def test_generation_rotation_preserves_private_source_mapping_settings_and_evidence(self):
        from dormguard.offline import rotate_generation

        with tempfile.TemporaryDirectory(prefix="dormguard-offline-rotation-") as directory:
            root = Path(directory)
            original = root / "original.mp4"
            original.write_bytes(b"Controlled opaque copy fixture; no video or inference claim")
            sources = [{"prepared_file": str(original), "name": "Source A", "scenario": "controlled-unit"}]
            previous_dir = prepare_generation(root, sources)
            previous = Store(previous_dir)
            try:
                user = previous.list_users()[0]
                previous.update_profile(user["id"], "Presentation Director")
                camera = previous.cameras()[0]
                previous.update_camera(
                    camera["id"], {"name": "Saved camera name", "enabled": True}, user["id"]
                )
                previous.create_incident(camera["id"], 0.8, "Controlled SQL-only reset regression")
                evidence = previous.evidence_dir / "retained-original.bin"
                evidence.write_bytes(b"preserve immutable original")
                fresh_dir = rotate_generation(root, previous_dir, sources)
                self.assertEqual(evidence.read_bytes(), b"preserve immutable original")
                self.assertEqual(len(previous.incidents()), 1)
                self.assertEqual(
                    original.read_bytes(), b"Controlled opaque copy fixture; no video or inference claim"
                )
                fresh = Store(fresh_dir)
                try:
                    self.assertEqual(fresh.incidents(), [])
                    self.assertEqual(fresh.list_users()[0]["display_name"], "Presentation Director")
                    self.assertEqual(fresh.cameras()[0]["name"], "Saved camera name")
                    self.assertFalse(fresh.cameras()[0]["enabled"])
                    self.assertTrue(fresh.verify_audit()["valid"])
                finally:
                    fresh.db.close()
            finally:
                previous.db.close()
