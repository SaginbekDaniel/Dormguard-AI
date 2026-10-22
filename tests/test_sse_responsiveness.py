"""Actual SSE generator regressions with disposable SQL and controlled slow reads."""

import asyncio
import hashlib
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

import httpx
from starlette.datastructures import State

from dormguard.app import COOKIE, create_app
from dormguard.config import Settings
from dormguard.security import token_hash
from dormguard.store import Store


class SSEResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="dormguard-sse-test-")
        self.addCleanup(self.directory.cleanup)
        settings = Settings(Path(self.directory.name), auth_mode="local", public_url="http://testserver")
        self.store = Store(settings.data_dir)
        self.addCleanup(self.store.db.close)
        user = self.store.provision_user("controlled@example.invalid", "director")
        self.token = "CONTROLLED_LOCAL_SSE_SESSION_TOKEN_42"
        with self.store.db.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,provider_token) VALUES(?,?,?,?,?)",
                (token_hash(self.token), user["id"], "CONTROLLED", time.time() + 60, None),
            )
        self.app = create_app(settings, self.store)
        self.endpoint = next(route.endpoint for route in self.app.routes if route.path == "/api/v1/events")

    def request(self):
        async def connected():
            return False

        return SimpleNamespace(cookies={COOKIE: self.token}, state=State(), is_disconnected=connected)

    async def test_slow_revision_snapshot_leaves_health_responsive_and_keeps_connection_in_one_thread(self):
        original_read = self.store.db.read
        with original_read() as conn:
            expected = [
                dict(
                    conn.execute("SELECT COUNT(*) AS n,MAX(created_at) AS stamp FROM audit_logs").fetchone()
                ),
                dict(conn.execute("SELECT MAX(updated_at) AS stamp FROM camera_health").fetchone()),
                dict(conn.execute("SELECT MAX(updated_at) AS stamp FROM worker_health").fetchone()),
            ]
        expected_revision = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()[:16]
        entered, release = threading.Event(), threading.Event()
        observations = {}
        loop_thread = threading.get_ident()

        class ControlledConnection:
            def __init__(self, connection):
                self.connection = connection
                self.open_thread = threading.get_ident()
                self.revision = False

            def execute(self, sql, params=()):
                if "SELECT COUNT(*) AS n,MAX(created_at) AS stamp FROM audit_logs" in sql:
                    self.revision = True
                    observations["open_thread"] = self.open_thread
                    observations["query_thread"] = threading.get_ident()
                    entered.set()
                    release.wait(timeout=2)
                return self.connection.execute(sql, params)

        @contextmanager
        def controlled_read():
            wrapped = None
            try:
                with original_read() as connection:
                    wrapped = ControlledConnection(connection)
                    yield wrapped
            finally:
                if wrapped is not None and wrapped.revision:
                    observations["close_thread"] = threading.get_ident()

        self.store.db.read = controlled_read
        response = await self.endpoint(self.request())
        # A separate timer bounds the fail-before case: a blocked event loop
        # cannot run an asyncio callback that releases its own SQL operation.
        timer = threading.Timer(0.6, release.set)
        timer.start()
        next_event = asyncio.create_task(response.body_iterator.__anext__())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
            ) as client:
                health = await client.get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertFalse(release.is_set(), "SSE SQL blocked unrelated API work until its timer expired")
            self.assertFalse(next_event.done())
            release.set()
            chunk = await asyncio.wait_for(next_event, timeout=2)
            self.assertEqual(
                chunk, "event: invalidate\ndata: " + json.dumps({"revision": expected_revision}) + "\n\n"
            )
            self.assertNotEqual(observations["query_thread"], loop_thread)
            self.assertEqual(observations["open_thread"], observations["query_thread"])
            self.assertEqual(observations["close_thread"], observations["query_thread"])
        finally:
            release.set()
            timer.cancel()
            with suppress(asyncio.CancelledError):
                await next_event
            await response.body_iterator.aclose()
            self.store.db.read = original_read

    async def test_revocation_before_next_snapshot_ends_stream_without_revision(self):
        response = await self.endpoint(self.request())
        with self.store.db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(self.token),))
        chunk = await response.body_iterator.__anext__()
        self.assertEqual(chunk, "event: auth-expired\ndata: {}\n\n")
        with self.assertRaises(StopAsyncIteration):
            await response.body_iterator.__anext__()
