"""Bounded adapter availability on disposable SQLite and explicit pool doubles."""

import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from dormguard.app import COOKIE, create_app
from dormguard.config import Settings
from dormguard.database import Database, DatabaseUnavailable
from dormguard.store import Store


class DatabaseAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="dormguard-db-availability-")
        self.addCleanup(self.directory.cleanup)
        self.db = Database(Path(self.directory.name))
        self.addCleanup(self.db.close)

    def hold_slots(self):
        for _ in range(8):
            self.assertTrue(self.db.slots.acquire(blocking=False))

    def release_slots(self):
        for _ in range(8):
            self.db.slots.release()

    def test_saturated_read_and_write_fail_with_deadline_then_recover(self):
        for method in (self.db.read, self.db.transaction):
            self.hold_slots()
            started = time.monotonic()
            try:
                with patch("dormguard.database.ACQUIRE_TIMEOUT_SECONDS", 0.03):
                    with self.assertRaises(DatabaseUnavailable), patch.object(self.db, "connect") as connect:
                        with method():
                            self.fail("Saturated adapter entered SQL body")
                    connect.assert_not_called()
            finally:
                self.release_slots()
            self.assertLess(time.monotonic() - started, 1)
            with self.db.read() as conn:
                self.assertEqual(conn.execute("SELECT 1 AS n").fetchone()["n"], 1)

    def test_real_slot_exhaustion_is_sanitized_http_503(self):
        store = Store(Path(self.directory.name))
        self.addCleanup(store.db.close)
        self.db = store.db
        app = create_app(
            Settings(Path(self.directory.name), auth_mode="local", public_url="http://testserver"), store
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            client.cookies.set(COOKIE, "CONTROLLED-opaque-cookie-reaches-SQL-lookup")
            self.hold_slots()
            try:
                with patch("dormguard.database.ACQUIRE_TIMEOUT_SECONDS", 0.03):
                    response = client.get("/api/v1/overview")
            finally:
                self.release_slots()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": str(DatabaseUnavailable())})
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/api/v1/overview").status_code, 401)

    def test_pool_acquisition_failures_are_sanitized_and_release_slots(self):
        for error in (PoolTimeout, TooManyRequests, PoolClosed):
            attempts = []

            class ControlledUnavailablePool:
                @contextmanager
                def connection(self, *, timeout):
                    attempts.append(timeout)
                    raise error("CONTROLLED sensitive connection detail")
                    yield

            with (
                patch.object(self.db, "postgres", True),
                patch.object(self.db, "_pool", ControlledUnavailablePool()),
            ):
                with self.assertRaises(DatabaseUnavailable) as caught:
                    with self.db.transaction():
                        self.fail("Unavailable pool entered transaction body")
            self.assertNotIn("sensitive", str(caught.exception))
            self.assertEqual(len(attempts), 1)
            self.assertGreater(attempts[0], 0)
            self.assertLessEqual(attempts[0], 10)
            self.hold_slots()
            self.release_slots()

    def test_pool_receives_only_remaining_acquisition_budget(self):
        attempts = []

        class ControlledSlowSlots:
            def acquire(self, *, timeout):
                time.sleep(0.03)
                return True

            def release(self):
                pass

        class ControlledUnavailablePool:
            @contextmanager
            def connection(self, *, timeout):
                attempts.append(timeout)
                raise PoolTimeout("CONTROLLED acquisition timeout")
                yield

        with (
            patch("dormguard.database.ACQUIRE_TIMEOUT_SECONDS", 0.08),
            patch.object(self.db, "slots", ControlledSlowSlots()),
            patch.object(self.db, "postgres", True),
            patch.object(self.db, "_pool", ControlledUnavailablePool()),
            self.assertRaises(DatabaseUnavailable),
        ):
            with self.db.read():
                self.fail("Unavailable pool entered read body")
        self.assertEqual(len(attempts), 1)
        self.assertGreater(attempts[0], 0)
        self.assertLess(attempts[0], 0.06)

    def test_transaction_body_is_not_replayed_and_failed_write_rolls_back(self):
        calls = []
        error = RuntimeError("CONTROLLED transaction body failure")
        with self.assertRaises(RuntimeError) as caught:
            with self.db.transaction() as conn:
                calls.append(1)
                conn.execute("INSERT INTO worker_health VALUES('controlled','test','ready','{}',1)")
                raise error
        self.assertIs(caught.exception, error)
        self.assertEqual(calls, [1])
        with self.db.read() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM worker_health").fetchone()["n"], 0)

    def test_close_rejects_new_work_but_borrowed_work_finishes(self):
        with self.db.read() as conn:
            self.db.close()
            self.assertEqual(conn.execute("SELECT 1 AS n").fetchone()["n"], 1)
        for method in (self.db.read, self.db.transaction):
            with self.assertRaises(DatabaseUnavailable):
                with method():
                    self.fail("Closed adapter accepted new work")
        # Raw connections retain their independent caller-owned lifecycle.
        raw = self.db.connect()
        try:
            self.assertEqual(raw.execute("SELECT 1 AS n").fetchone()["n"], 1)
        finally:
            raw.close()


if __name__ == "__main__":
    unittest.main()
