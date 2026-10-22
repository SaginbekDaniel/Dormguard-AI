"""Disposable real SQL/API coverage; Telegram transport is an explicit test double."""

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dormguard.app import create_app
from dormguard.config import Settings
from dormguard.security import hash_password
from dormguard.store import Store
from dormguard.transport import RemoteError


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dormguard-experience-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name), auth_mode="local", public_url="http://testserver")
        self.store = Store(self.settings.data_dir)
        self.user = self.store.provision_user(
            "director@example.invalid", "director", hash_password("controlled-test-password-42")
        )
        self.camera = self.store.create_camera("North Hall", "Floor 2", "test", "REF", self.user["id"])
        self.client = TestClient(create_app(self.settings, self.store))
        self.addCleanup(self.client.close)
        self.csrf = self.client.post(
            "/api/v1/auth/login",
            json={"email": self.user["email"], "password": "controlled-test-password-42"},
        ).json()["csrf_token"]

    def test_full_history_search_dates_pagination_and_stable_order(self):
        for index in range(205):
            self.store.create_incident(
                self.camera["id"],
                0.81,
                "CONTROLLED_TEST_MODEL",
                detected_at="2026-10-01T12:00:00+00:00",
                cooldown_seconds=0,
                event_key="test-" + str(index),
            )
        params = {"limit": 30, "q": "North Hall", "from_date": "2026-10-01", "to_date": "2026-10-01"}
        seen = []
        for offset in range(0, 205, 30):
            body = self.client.get("/api/v1/incidents", params=params | {"offset": offset}).json()
            self.assertEqual(body["total"], 205)
            seen.extend(item["id"] for item in body["items"])
        self.assertEqual(len(set(seen)), 205)
        self.assertEqual(seen, sorted(seen, reverse=True))
        for params in (
            {"q": "' OR 1=1 --"},
            {"q": "%"},
            {"q": "_"},
            {"from_date": "2026-10-02"},
            {"min_score": 0.9},
        ):
            self.assertEqual(self.client.get("/api/v1/incidents", params=params).json()["total"], 0)
        for params in (
            {"from_date": "garbage"},
            {"from_date": "2026-10-02", "to_date": "2026-10-01"},
            {"offset": -1},
            {"sort": "DROP TABLE"},
        ):
            self.assertEqual(self.client.get("/api/v1/incidents", params=params).status_code, 422)

    def test_journal_search_and_real_camera_worker_transitions(self):
        self.store.set_camera_health(self.camera["id"], "connected", time.time())
        self.store.set_camera_health(self.camera["id"], "connected", time.time())
        self.store.set_camera_health(self.camera["id"], "disconnected")
        self.store.heartbeat("inference", "blocked")
        self.store.heartbeat("inference", "ready")
        body = self.client.get("/api/v1/audit", params={"q": self.camera["id"], "limit": 1}).json()
        self.assertEqual(body["total"], 3)  # registration + two actual connectivity changes
        self.assertEqual(len(body["items"]), 1)
        self.assertTrue(body["integrity"]["valid"])
        body = self.client.get(
            "/api/v1/audit", params={"category": "operations", "q": "worker.state_changed"}
        ).json()
        self.assertEqual(body["total"], 2)

    def test_overview_today_uses_utc_and_evidence_metadata(self):
        old = self.store.create_incident(
            self.camera["id"],
            0.8,
            "CONTROLLED_TEST_MODEL",
            detected_at="2026-01-01T00:00:00Z",
            cooldown_seconds=0,
        )
        self.store.create_incident(self.camera["id"], 0.8, "CONTROLLED_TEST_MODEL", cooldown_seconds=0)
        body = self.client.get("/api/v1/overview").json()
        self.assertEqual(body["incidents_today"], 1)
        self.assertEqual(sum(row["count"] for row in body["today_states"]), 1)
        self.assertEqual(body["recent_incidents"][0]["evidence_count"], 0)
        self.assertEqual(self.client.get("/api/v1/incidents", params={"q": old["id"]}).json()["total"], 1)

    def test_telegram_test_requires_explicit_csrf_bound_official_and_truthful_result(self):
        settings = replace(
            self.settings, telegram_bot_token="CONTROLLED_TOKEN", telegram_webhook_secret="CONTROLLED_SECRET"
        )
        with TestClient(create_app(settings, self.store)) as client:
            csrf = client.post(
                "/api/v1/auth/login",
                json={"email": self.user["email"], "password": "controlled-test-password-42"},
            ).json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf}
            with patch("dormguard.notifications.request_json") as transport:
                self.assertEqual(
                    client.post("/api/v1/integrations/telegram/test", json={"confirm": True}).status_code, 403
                )
                self.assertEqual(
                    client.post("/api/v1/integrations/telegram/test", headers=headers, json={}).status_code,
                    422,
                )
                self.assertEqual(
                    client.post(
                        "/api/v1/integrations/telegram/test", headers=headers, json={"confirm": True}
                    ).status_code,
                    409,
                )
                transport.assert_not_called()
                with self.store.db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO telegram_bindings(user_id,chat_id,created_at) VALUES(?,?,?)",
                        (self.user["id"], "12345", "2026-10-01T00:00:00Z"),
                    )
                transport.return_value = {"ok": True, "result": {"message_id": 42}}
                result = client.post(
                    "/api/v1/integrations/telegram/test", headers=headers, json={"confirm": True}
                )
                self.assertTrue(result.json()["accepted"])
                self.assertEqual(result.json()["device_delivery"], "unverified")
                self.assertEqual(transport.call_args.kwargs["body"]["chat_id"], "12345")
                self.assertNotIn("12345", result.text)
                transport.side_effect = RemoteError(503)
                self.assertEqual(
                    client.post(
                        "/api/v1/integrations/telegram/test", headers=headers, json={"confirm": True}
                    ).status_code,
                    503,
                )
                self.assertEqual(
                    client.post(
                        "/api/v1/integrations/telegram/test", headers=headers, json={"confirm": True}
                    ).status_code,
                    429,
                )
            self.assertTrue(self.store.verify_audit()["valid"])
            self.assertEqual(
                len([r for r in self.store.audits() if r["action"] == "telegram.test_accepted"]), 1
            )
            self.assertEqual(self.store.overview()["total_incidents"], 0)

    def test_incident_delivery_history_is_scoped_and_sensitive_data_is_absent(self):
        other = self.store.provision_user("deputy@example.invalid", "deputy_director")
        incident = self.store.create_incident(self.camera["id"], 0.8, "CONTROLLED_TEST_MODEL")
        result = self.client.get("/api/v1/incidents/" + incident["id"])
        self.assertEqual(len(result.json()["notifications"]), 1)
        self.assertNotIn(other["id"], str(result.json()["notifications"]))
        self.assertNotIn("source_ref", result.text)
        self.assertNotIn("chat_id", result.text)
        self.assertNotIn("lease_token", result.text)
        self.assertIn("audit_events", result.json())
        self.client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": self.csrf})
        for path in ("/api/v1/incidents?q=x", "/api/v1/audit?q=x", "/api/v1/system"):
            self.assertEqual(self.client.get(path).status_code, 401)
