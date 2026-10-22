"""Principal-review regressions. Provider and encoder outputs are controlled doubles."""

import asyncio
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from dormguard import notifications, playback
from dormguard.app import COOKIE, create_app
from dormguard.config import Settings
from dormguard.security import token_hash
from dormguard.store import DomainError, Store
from dormguard.transport import RemoteError, request_json


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dormguard-review-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.director = self.store.provision_user("director@example.test", "director")["id"]
        self.deputy = self.store.provision_user("deputy@example.test", "deputy_director")["id"]
        self.settings = Settings(
            Path(self.temp.name), auth_mode="local", telegram_bot_token="CONTROLLED-TEST-TOKEN"
        )
        camera = self.store.create_camera(
            "Controlled source", "Location A", "test", "DORMGUARD_CAMERA_TEST", self.director
        )
        self.incident = self.store.create_incident(camera["id"], 0.8, "CONTROLLED TEST DOUBLE")
        with self.store.db.transaction() as conn:
            for user, chat in ((self.director, "101"), (self.deputy, "102")):
                conn.execute(
                    "INSERT INTO telegram_bindings(user_id,chat_id,created_at) VALUES(?,?,?)",
                    (user, chat, "controlled"),
                )

    def test_unlinked_after_claim_before_send_is_cancelled(self):
        original = self.store.incident
        calls = []

        def incident_hook(identity):
            notifications.unlink(self.store, self.director)
            notifications.unlink(self.store, self.deputy)
            return original(identity)

        def controlled_transport(*args, **kwargs):
            calls.append(kwargs["body"])
            return {"ok": True, "result": {"message_id": 1}}

        worker = notifications.TelegramWorker(self.store, self.settings, controlled_transport)
        with patch.object(self.store, "incident", side_effect=incident_hook):
            worker.run_once()
        self.assertEqual(calls, [], "A revoked binding must be rechecked before an unstarted provider send")

    def test_concurrent_same_original_publishes_one_valid_derivative(self):
        source = Path(self.temp.name) / "controlled.bin"
        source.write_bytes(b"CONTROLLED original fixture; not a video")
        evidence = self.store.add_evidence(self.incident["id"], source, 0, 1)
        item = self.store.evidence(evidence["id"], True)
        calls = []
        lock = threading.Lock()

        def controlled_encoder(source, target):
            with lock:
                value = len(calls)
                calls.append(value)
            time.sleep(0.05)
            Path(target).write_bytes(b"CONTROLLED encoded fixture " + str(value).encode())

        with (
            patch("dormguard.playback._transcode", side_effect=controlled_encoder),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(lambda _: playback.browser_derivative(self.store, item), range(2)))
        self.assertEqual(len(calls), 1, "Same original must have one concurrent derivative publisher")
        self.assertEqual(results[0], results[1])
        self.assertEqual(
            playback.digest_file(results[0]), results[0].with_suffix(".sha256").read_text().strip()
        )

    def test_malformed_provider_result_records_failure_without_worker_crash(self):
        for malformed in (
            None,
            [],
            {"ok": True, "result": None},
            {"ok": True, "result": []},
            {"ok": "false", "result": {"message_id": True}},
        ):
            worker = notifications.TelegramWorker(
                self.store, self.settings, lambda *args, **kwargs: malformed
            )
            self.assertTrue(worker.run_once())
            with self.store.db.transaction() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM notification_outbox WHERE state='accepted'"
                    ).fetchone()["n"],
                    0,
                )
                conn.execute(
                    "UPDATE notification_outbox SET state='queued',next_attempt=0 WHERE state='queued'"
                )

    def test_malformed_webhook_nested_shapes_fail_closed(self):
        settings = replace(self.settings, telegram_webhook_secret="CONTROLLED-SECRET")
        for payload in (
            {"message": None},
            {"message": []},
            {"message": {"chat": None}},
            {"message": {"chat": {}, "from": []}},
            {"message": {"chat": [], "from": {}}},
        ):
            with self.subTest(payload=payload), self.assertRaises(DomainError) as error:
                notifications.bind_webhook(self.store, settings, "CONTROLLED-SECRET", payload)
            self.assertEqual(error.exception.status, 422)

    def test_provider_transport_rejects_nonobject_json(self):
        class ControlledResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return b"[]"

        with patch("dormguard.transport.build_opener") as opener:
            opener.return_value.open.return_value = ControlledResponse()
            with self.assertRaises(RemoteError):
                request_json("GET", "https://provider.invalid/controlled")

    def test_disabled_after_claim_before_send_is_blocked(self):
        original = self.store.incident
        calls = []

        def incident_hook(identity):
            with self.store.db.transaction() as conn:
                conn.execute("UPDATE users SET enabled=0")
            return original(identity)

        worker = notifications.TelegramWorker(
            self.store, self.settings, lambda *args, **kwargs: calls.append(kwargs["body"])
        )
        with patch.object(self.store, "incident", side_effect=incident_hook):
            worker.run_once()
        self.assertEqual(calls, [])

    def test_rebound_after_claim_uses_current_binding(self):
        original = self.store.incident
        calls = []

        def incident_hook(identity):
            with self.store.db.transaction() as conn:
                conn.execute("UPDATE telegram_bindings SET chat_id='new-'||chat_id")
            return original(identity)

        worker = notifications.TelegramWorker(
            self.store,
            self.settings,
            lambda *args, **kwargs: calls.append(kwargs["body"]) or {"ok": True, "result": {"message_id": 1}},
        )
        with patch.object(self.store, "incident", side_effect=incident_hook):
            worker.run_once()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["chat_id"].startswith("new-"))

    def test_chunked_oversized_login_body_is_bounded(self):
        with TestClient(create_app(self.settings, self.store)) as client:

            def chunks():
                yield b'{"email":"'
                for _ in range(18):
                    yield b"a" * 65536
                yield b'","password":"controlled"}'

            response = client.post(
                "/api/v1/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
            )
            self.assertEqual(response.status_code, 413)

    def _stream_reauth(self, auth_mode, revoke):
        """Drive the actual route generator with byte frames and controlled identity."""
        token, ticket = "controlled-session-token-abcdefghijklmnopqrstuvwxyz", "controlled-ticket"
        camera_id = self.incident["camera_id"]
        with self.store.db.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,provider_token) VALUES(?,?,?,?,?)",
                (
                    token_hash(token),
                    self.director,
                    "controlled-csrf",
                    time.time() + 600,
                    "controlled-provider-token",
                ),
            )
            conn.execute(
                "INSERT INTO stream_tickets(token_hash,user_id,camera_id,session_hash,expires_at) VALUES(?,?,?,?,?)",
                (token_hash(ticket), self.director, camera_id, token_hash(token), time.time() + 60),
            )
        directory = self.settings.data_dir / "frames"
        directory.mkdir()
        (directory / (camera_id + ".jpg")).write_bytes(b"CONTROLLED BYTE FRAME; not actual JPEG")

        class ControlledProvider:
            failure = False

            def validate(provider, token):
                if provider.failure:
                    raise RemoteError(503)
                return {"id": self.director}

        provider = ControlledProvider()
        settings = replace(self.settings, auth_mode=auth_mode)
        app = create_app(settings, self.store, provider)

        async def connected():
            return False

        request = SimpleNamespace(
            cookies={COOKIE: token}, headers={}, state=SimpleNamespace(), is_disconnected=connected
        )
        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/v1/streams/{ticket}")
        response = endpoint(ticket, request)

        async def exercise():
            first = await response.body_iterator.__anext__()
            self.assertIn(b"CONTROLLED BYTE FRAME", first)
            revoke(provider)
            if auth_mode == "supabase":
                # Production intentionally checks provider at most every five
                # seconds; local SQL revocation is checked on every frame.
                await asyncio.sleep(5.05)
            with self.assertRaises(StopAsyncIteration):
                await response.body_iterator.__anext__()

        asyncio.run(exercise())

    def test_ongoing_stream_stops_after_local_session_revocation(self):
        def revoke(provider):
            with self.store.db.transaction() as conn:
                conn.execute("DELETE FROM sessions")

        self._stream_reauth("local", revoke)

    def test_ongoing_stream_stops_during_provider_outage(self):
        self._stream_reauth("supabase", lambda provider: setattr(provider, "failure", True))


if __name__ == "__main__":
    unittest.main()
