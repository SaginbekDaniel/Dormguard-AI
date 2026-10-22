"""Disposable API/provider tests. Controlled doubles are never production detectors/providers."""

import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dormguard import notifications
from dormguard.app import COOKIE, create_app
from dormguard.config import Settings
from dormguard.notifications import TelegramWorker
from dormguard.security import hash_password, resolve_source
from dormguard.store import DomainError, Store
from dormguard.transport import RemoteError

PASSWORD = "controlled-test-password-42"


class ControlledAuth:
    """Explicit Supabase contract double; no external Auth operation."""

    configured = True

    def __init__(self, user):
        self.user = user
        self.failure = None
        self.logout_failure = None

    def login(self, email, password):
        return {
            "user": {"id": self.user["id"], "user_metadata": {"role": "director"}},
            "access_token": "controlled-test-provider-token",
            "expires_in": 3600,
        }

    def validate(self, token):
        if self.failure:
            raise self.failure
        return {"id": self.user["id"]}

    def logout(self, token):
        if self.logout_failure:
            raise self.logout_failure


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dormguard-api-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name), auth_mode="local", public_url="http://testserver")
        self.store = Store(self.settings.data_dir)
        self.director = self.store.provision_user(
            "director@example.invalid", "director", hash_password(PASSWORD)
        )
        self.deputy = self.store.provision_user(
            "deputy@example.invalid", "deputy_director", hash_password(PASSWORD)
        )
        self.admin = self.store.provision_user(
            "admin@example.invalid", "technical_admin", hash_password(PASSWORD)
        )
        self.client = TestClient(create_app(self.settings, self.store))
        self.addCleanup(self.client.close)

    def login(self, client=None, role="director"):
        client = client or self.client
        response = client.post(
            "/api/v1/auth/login", json={"email": role + "@example.invalid", "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": response.json()["csrf_token"]}

    def camera(self):
        return self.store.create_camera(
            "Hall A", "Ground floor", "test", "DORMGUARD_CAMERA_TEST", self.director["id"]
        )

    def test_anonymous_deny_matrix_and_no_signup(self):
        for path in (
            "overview",
            "cameras",
            "incidents",
            "evidence",
            "system",
            "audit",
            "notifications",
            "integrations",
            "auth/me",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get("/api/v1/" + path).status_code, 401)
        self.assertFalse(self.client.get("/api/v1/auth/config").json()["signup_enabled"])
        self.assertEqual(self.client.post("/api/v1/auth/signup", json={}).status_code, 404)
        self.assertEqual(self.client.get("/docs").status_code, 404)

    def test_session_cookie_csrf_logout_and_headers(self):
        csrf = self.login()
        old_cookie = self.client.cookies.get(COOKIE)
        me = self.client.get("/api/v1/auth/me")
        self.assertEqual(me.json()["role"], "director")
        self.assertNotIn("password_hash", me.text)
        self.assertIn("no-store", me.headers["cache-control"])
        self.assertIn("frame-ancestors 'none'", me.headers["content-security-policy"])
        self.assertEqual(self.client.post("/api/v1/auth/logout").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 200)
        response = self.client.post("/api/v1/auth/logout", headers=csrf)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.client.cookies.set(COOKIE, old_cookie)
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 401)

    def test_technical_admin_never_gets_platform_session(self):
        response = self.client.post(
            "/api/v1/auth/login", json={"email": self.admin["email"], "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 401)
        with self.store.db.read() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"], 0)

    def test_disabled_and_expired_accounts_fail_closed(self):
        self.login()
        with self.store.db.transaction() as conn:
            conn.execute("UPDATE users SET enabled=0 WHERE id=?", (self.director["id"],))
        self.assertEqual(self.client.get("/api/v1/incidents").status_code, 401)
        with self.store.db.transaction() as conn:
            conn.execute("UPDATE users SET enabled=1 WHERE id=?", (self.director["id"],))
            conn.execute("UPDATE sessions SET expires_at=?", (time.time() - 1,))
        self.assertEqual(self.client.get("/api/v1/incidents").status_code, 401)

    def test_cross_origin_login_and_writes_denied(self):
        response = self.client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://attacker.invalid"},
            json={"email": self.director["email"], "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 403)
        csrf = self.login()
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/logout", headers={**csrf, "Origin": "https://attacker.invalid"}
            ).status_code,
            403,
        )

    def test_director_camera_registration_and_deputy_denial(self):
        csrf = self.login()
        body = {"name": "Hall", "location": "Floor 1", "kind": "test", "source_ref": "DORMGUARD_CAMERA_TEST"}
        self.assertEqual(self.client.post("/api/v1/cameras", json=body).status_code, 403)
        response = self.client.post("/api/v1/cameras", json=body, headers=csrf)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("source_ref", response.text)
        self.assertNotIn("source_ref", self.client.get("/api/v1/cameras").text)
        with TestClient(create_app(self.settings, self.store)) as deputy_client:
            deputy_csrf = self.login(deputy_client, "deputy")
            self.assertEqual(
                deputy_client.post("/api/v1/cameras", json=body, headers=deputy_csrf).status_code, 403
            )
            self.assertEqual(deputy_client.get("/api/v1/cameras").status_code, 200)
        body["source_ref"] = "rtsp://private:password@127.0.0.1"
        self.assertEqual(self.client.post("/api/v1/cameras", json=body, headers=csrf).status_code, 422)

    def test_incident_workflow_real_sql_and_notification_isolation(self):
        camera = self.camera()
        incident = self.store.create_incident(
            camera["id"], 0.8, "CONTROLLED TEST MODEL; not inference", event_key="controlled-event"
        )
        csrf = self.login()
        detail = self.client.get("/api/v1/incidents/" + incident["id"])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "DETECTED")
        endpoint = "/api/v1/incidents/" + incident["id"]
        self.assertEqual(self.client.post(endpoint + "/confirm", headers=csrf, json={}).status_code, 409)
        for action in ("acknowledge", "confirm", "resolve"):
            self.assertEqual(
                self.client.post(endpoint + "/" + action, headers=csrf, json={}).status_code, 200
            )
        self.assertEqual(self.client.post(endpoint + "/close", headers=csrf, json={}).status_code, 422)
        final = self.client.post(endpoint + "/close", headers=csrf, json={"reason": "Human review complete"})
        self.assertEqual(final.json()["status"], "CLOSED")
        own = self.client.get("/api/v1/notifications").json()["items"]
        self.assertEqual(len(own), 1)
        with self.store.db.read() as conn:
            other = conn.execute(
                "SELECT id FROM notification_outbox WHERE user_id=?", (self.deputy["id"],)
            ).fetchone()["id"]
        self.assertEqual(
            self.client.post("/api/v1/notifications/" + other + "/retry", headers=csrf).status_code, 404
        )
        self.assertTrue(self.client.get("/api/v1/audit").json()["integrity"]["valid"])

    def test_evidence_tamper_prevents_video_and_download(self):
        source = Path(self.temp.name) / "controlled.bin"
        source.write_bytes(b"controlled evidence bytes, not video")
        incident = self.store.create_incident(
            self.camera()["id"],
            0.8,
            "CONTROLLED TEST DOUBLE",
            evidence_source=source,
            clip_start=time.time() - 5,
            clip_end=time.time(),
            event_key="evidence",
        )
        evidence_id = incident["evidence"][0]["id"]
        csrf = self.login()
        self.assertNotIn("path", self.client.get("/api/v1/evidence").text)
        self.assertTrue(
            self.client.post(f"/api/v1/evidence/{evidence_id}/verify", headers=csrf).json()["valid"]
        )
        path = self.store.evidence_path(self.store.evidence(evidence_id, private=True))
        os.chmod(path, 0o600)
        path.write_bytes(b"tampered")
        self.assertFalse(
            self.client.post(f"/api/v1/evidence/{evidence_id}/verify", headers=csrf).json()["valid"]
        )
        for action in ("video", "download"):
            self.assertEqual(self.client.get(f"/api/v1/evidence/{evidence_id}/{action}").status_code, 409)

    def test_stream_ticket_bound_to_user_session_and_missing_stream(self):
        camera = self.camera()
        csrf = self.login()
        response = self.client.post(f"/api/v1/cameras/{camera['id']}/stream-session", headers=csrf)
        self.assertEqual(response.status_code, 200)
        url = response.json()["url"]
        with TestClient(create_app(self.settings, self.store)) as other:
            self.login(other, "deputy")
            self.assertEqual(other.get(url).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 503)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_rate_limit_and_login_failure(self):
        for _ in range(8):
            self.assertEqual(
                self.client.post(
                    "/api/v1/auth/login", json={"email": "missing@example.invalid", "password": "wrong"}
                ).status_code,
                401,
            )
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/login", json={"email": "missing@example.invalid", "password": "wrong"}
            ).status_code,
            429,
        )

    def test_supabase_outage_logout_revokes_locally_k01_matrix(self):
        for status in (401, 503, 504):
            with self.subTest(provider_status=status):
                provider = ControlledAuth(self.director)
                settings = replace(self.settings, auth_mode="supabase")
                with TestClient(create_app(settings, self.store, provider)) as client:
                    csrf = self.login(client)
                    cookie = client.cookies.get(COOKIE)
                    provider.failure = RemoteError(status)
                    provider.logout_failure = RemoteError(status)
                    self.assertIn(client.get("/api/v1/incidents").status_code, (401, 503))
                    self.assertEqual(client.post("/api/v1/auth/logout").status_code, 403)
                    response = client.post("/api/v1/auth/logout", headers=csrf)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["provider_signout"], "unavailable_local_session_revoked")
                    provider.failure = None
                    client.cookies.set(COOKIE, cookie)
                    self.assertEqual(client.get("/api/v1/auth/me").status_code, 401)

    def test_supabase_metadata_does_not_authorize_unassigned_or_technical_admin(self):
        for identity in ({"id": "unassigned-id"}, self.admin):
            provider = ControlledAuth(identity)
            with TestClient(
                create_app(replace(self.settings, auth_mode="supabase"), self.store, provider)
            ) as client:
                response = client.post(
                    "/api/v1/auth/login", json={"email": self.director["email"], "password": PASSWORD}
                )
                self.assertEqual(response.status_code, 401)

    def test_supabase_identity_change_and_normal_logout(self):
        provider = ControlledAuth(self.director)
        with TestClient(
            create_app(replace(self.settings, auth_mode="supabase"), self.store, provider)
        ) as client:
            csrf = self.login(client)
            provider.user = self.deputy
            self.assertEqual(client.get("/api/v1/incidents").status_code, 401)
            provider.user = self.director
            self.assertEqual(
                client.post("/api/v1/auth/logout", headers=csrf).json()["provider_signout"], "requested"
            )


class SourceTests(unittest.TestCase):
    def test_rtsp_literal_allowlist_and_no_credentials_in_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory), camera_networks="127.0.0.0/8,10.0.0.0/8")
            camera = {"kind": "rtsp", "source_ref": "DORMGUARD_CAMERA_TEST"}
            with patch.dict(
                os.environ, {"DORMGUARD_CAMERA_TEST": "rtsp://private:secret@127.0.0.1:8554/live"}
            ):
                self.assertTrue(resolve_source(settings, camera).startswith("rtsp://"))
            for source in (
                "rtsp://secret@169.254.1.1/live",
                "rtsp://secret@localhost/live",
                "http://10.0.0.2/live",
                "rtsp://10.0.0.1:bad/live",
                "rtsp://224.0.0.2/live",
            ):
                with patch.dict(os.environ, {"DORMGUARD_CAMERA_TEST": source}):
                    with self.assertRaises(ValueError) as error:
                        resolve_source(settings, camera)
                    self.assertNotIn("secret", str(error.exception))

    def test_test_source_cannot_escape_private_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory))
            outside = Path(directory) / "outside.mp4"
            outside.write_bytes(b"not footage")
            with patch.dict(os.environ, {"DORMGUARD_CAMERA_TEST": str(outside)}):
                with self.assertRaises(ValueError):
                    resolve_source(settings, {"kind": "test", "source_ref": "DORMGUARD_CAMERA_TEST"})

    def test_production_and_numeric_configuration_fail_closed(self):
        with self.assertRaises(ValueError):
            Settings(Path("/tmp/unused"), environment="production", auth_mode="local")
        for field, value in (
            ("inference_fps", float("nan")),
            ("max_inference_queue", 0),
            ("detection_threshold", float("inf")),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                Settings(Path("/tmp/unused"), **{field: value})


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dormguard-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            Path(self.temp.name),
            auth_mode="local",
            telegram_bot_token="CONTROLLED-TEST-TOKEN",
            telegram_bot_username="controlled_bot",
            telegram_webhook_secret="CONTROLLED-TEST-SECRET",
        )
        self.store = Store(self.settings.data_dir)
        self.director = self.store.provision_user("director@example.invalid", "director")
        self.deputy = self.store.provision_user("deputy@example.invalid", "deputy_director")
        camera = self.store.create_camera(
            "Hall A", "Floor 1", "test", "DORMGUARD_CAMERA_TEST", self.director["id"]
        )
        self.incident = self.store.create_incident(
            camera["id"], 0.8, "CONTROLLED TEST DOUBLE", event_key="delivery"
        )

    def bind(self, user, chat_id):
        result = notifications.create_link(self.store, user["id"], self.settings)
        payload = {
            "message": {
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": chat_id},
                "text": "/start " + result["token"],
            }
        }
        return (
            result,
            payload,
            notifications.bind_webhook(
                self.store, self.settings, self.settings.telegram_webhook_secret, payload
            ),
        )

    def test_private_binding_secret_single_use_and_unique_recipient(self):
        link = notifications.create_link(self.store, self.director["id"], self.settings)
        payload = {
            "message": {
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42},
                "text": "/start " + link["token"],
            }
        }
        with self.assertRaises(DomainError):
            notifications.bind_webhook(self.store, self.settings, "wrong", payload)
        self.assertTrue(
            notifications.bind_webhook(
                self.store, self.settings, self.settings.telegram_webhook_secret, payload
            )["linked"]
        )
        with self.assertRaises(DomainError):
            notifications.bind_webhook(
                self.store, self.settings, self.settings.telegram_webhook_secret, payload
            )
        other = notifications.create_link(self.store, self.deputy["id"], self.settings)
        payload["message"]["text"] = "/start " + other["token"]
        with self.assertRaises(DomainError):
            notifications.bind_webhook(
                self.store, self.settings, self.settings.telegram_webhook_secret, payload
            )

    def test_retry_429_backoff_and_acceptance_separate_from_incident_state(self):
        self.bind(self.director, 101)
        self.bind(self.deputy, 102)
        calls = []

        def controlled_transport(method, url, body):
            calls.append(body)
            if len(calls) == 1:
                raise RemoteError(429, 120)
            return {"ok": True, "result": {"message_id": 123}}

        worker = TelegramWorker(self.store, self.settings, controlled_transport)
        self.assertTrue(worker.run_once())
        with self.store.db.read() as conn:
            delayed = conn.execute(
                "SELECT * FROM notification_outbox WHERE last_error='provider_429'"
            ).fetchone()
            self.assertGreater(delayed["next_attempt"], time.time() + 100)
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        self.assertEqual(self.store.incident(self.incident["id"])["status"], "DETECTED")
        self.assertNotIn("rtsp", calls[-1]["text"])
        self.assertIn("/#incidents/", calls[-1]["text"])

    def test_missing_bot_disabled_and_unlinked_never_send(self):
        self.bind(self.director, 101)
        with self.store.db.transaction() as conn:
            conn.execute("UPDATE users SET enabled=0 WHERE id=?", (self.director["id"],))

        def no_send(*args, **kwargs):
            self.fail("External transport must not be called")

        worker = TelegramWorker(self.store, self.settings, no_send)
        self.assertTrue(worker.run_once())
        self.assertTrue(worker.run_once())
        with self.store.db.read() as conn:
            states = [
                row["state"] for row in conn.execute("SELECT state FROM notification_outbox").fetchall()
            ]
        self.assertEqual(states, ["blocked", "blocked"])

    def test_terminal_error_and_six_attempt_bound(self):
        self.bind(self.director, 101)
        self.bind(self.deputy, 102)

        def controlled_error(*args, **kwargs):
            raise RemoteError(403)

        worker = TelegramWorker(self.store, self.settings, controlled_error)
        worker.run_once()
        with self.store.db.read() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM notification_outbox WHERE state='failed'").fetchone()[
                    "n"
                ],
                1,
            )
        with self.store.db.transaction() as conn:
            conn.execute("UPDATE notification_outbox SET state='queued',attempts=5,next_attempt=0")

        def transient(*args, **kwargs):
            raise RemoteError(503)

        worker.transport = transient
        worker.run_once()
        worker.run_once()
        self.assertFalse(worker.run_once())
        with self.store.db.read() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM notification_outbox WHERE state='failed' AND attempts=6"
                ).fetchone()["n"],
                2,
            )

    def test_expired_lease_recovers_and_old_owner_cannot_finish(self):
        self.bind(self.director, 101)
        self.bind(self.deputy, 102)
        worker = TelegramWorker(self.store, self.settings)
        original = worker._claim()
        with self.store.db.transaction() as conn:
            conn.execute("UPDATE notification_outbox SET lease_until=0 WHERE id=?", (original["id"],))
        replacement = worker._claim()
        self.assertEqual(original["id"], replacement["id"])
        worker._finish(original, "accepted", None, "old-provider-id")
        with self.store.db.read() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT state FROM notification_outbox WHERE id=?", (original["id"],)
                ).fetchone()["state"],
                "sending",
            )
        worker._finish(replacement, "accepted", None, "new-provider-id")
        other_user_id = (
            self.deputy["id"] if original["user_id"] == self.director["id"] else self.director["id"]
        )
        notifications.unlink(self.store, other_user_id)
        with self.store.db.read() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT state FROM notification_outbox WHERE user_id=?", (other_user_id,)
                ).fetchone()["state"],
                "cancelled",
            )


class TransportTests(unittest.TestCase):
    def test_rate_limit_body_read_failure_remains_retryable(self):
        from urllib.error import HTTPError

        from dormguard.transport import request_json

        class ControlledBrokenResponse:
            def read(self, size=-1):
                raise OSError("controlled interrupted provider response")

            def close(self):
                pass

        error = HTTPError(
            "https://provider.invalid/CONTROLLED_SECRET", 429, "rate limit", {}, ControlledBrokenResponse()
        )
        with patch("dormguard.transport.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(RemoteError) as raised:
                request_json("POST", "https://provider.invalid/test")
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.retry_after, 30)
        self.assertNotIn("CONTROLLED_SECRET", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
