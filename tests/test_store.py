"""Disposable SQLite integration tests; byte fixtures are not inference/video claims."""

import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from dormguard.store import DomainError, Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dormguard-store-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "private")
        self.director = self.store.provision_user("director@example.test", "director")["id"]
        self.deputy = self.store.provision_user("deputy@example.test", "deputy_director")["id"]
        self.admin = self.store.provision_user("admin@example.test", "technical_admin")["id"]
        self.camera = self.store.create_camera(
            "Test source", "Lab", "test", "/private/test.mp4", self.director
        )["id"]

    def incident(self, **kwargs):
        return self.store.create_incident(self.camera, 0.9, "controlled-test-model", **kwargs)

    def test_atomic_creation_disabled_recipient_and_privacy(self):
        with self.store.db.transaction() as conn:
            conn.execute("UPDATE users SET enabled=0 WHERE id=?", (self.deputy,))
        incident = self.incident()
        with self.store.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM notification_outbox WHERE incident_id=?", (incident["id"],)
            ).fetchall()
        self.assertEqual({row["user_id"] for row in rows}, {self.director, self.deputy})
        self.assertTrue(all(row["state"] == "queued" for row in rows))
        self.assertNotIn("source_ref", self.store.camera(self.camera))
        self.assertEqual(self.store.camera(self.camera, True)["source_ref"], "/private/test.mp4")
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_concurrent_cooldown_and_permanent_correlated_key(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: self.incident(event_key="event-" + str(n)), range(20)))
        self.assertEqual(len({row["id"] for row in results}), 1)
        first = results[0]["id"]
        later = self.incident(detected_at=time.time() - 1000, event_key="distinct-old", cooldown_seconds=1)
        retry = self.incident(detected_at=time.time() - 1000, event_key="event-19")
        self.assertEqual(retry["id"], first)
        self.assertNotEqual(later["id"], first)
        with self.store.db.read() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM notification_outbox").fetchone()["n"], 4)
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_all_legal_edges_and_forbidden_transitions(self):
        routes = [
            (["review", "acknowledge", "confirm", "resolve", "close"], "CLOSED"),
            (["acknowledge", "respond", "resolve", "close"], "CLOSED"),
            (["false-positive"], "FALSE_POSITIVE"),
            (["review", "false-positive"], "FALSE_POSITIVE"),
            (["acknowledge", "false-positive"], "FALSE_POSITIVE"),
            (["acknowledge", "respond", "false-positive"], "FALSE_POSITIVE"),
        ]
        for actions, target in routes:
            incident = self.incident(cooldown_seconds=0)
            for action in actions:
                incident = self.store.transition(incident["id"], action, self.deputy, "Reviewed test case")
            self.assertEqual(incident["status"], target)
            with self.assertRaises(DomainError):
                self.store.transition(incident["id"], "acknowledge", self.director)
        incident = self.incident(cooldown_seconds=0)
        for action in ("resolve", "confirm", "close"):
            with self.assertRaises(DomainError):
                self.store.transition(incident["id"], action, self.director, "reason")
        with self.assertRaises(DomainError) as reason:
            self.store.transition(incident["id"], "false-positive", self.director)
        self.assertEqual(reason.exception.status, 422)
        self.store.transition(incident["id"], "acknowledge", self.director)
        self.store.transition(incident["id"], "confirm", self.director)
        with self.assertRaises(DomainError):
            self.store.transition(incident["id"], "false-positive", self.director, "reason")

    def test_concurrent_transition_single_winner(self):
        incident = self.incident()

        def attempt(_):
            try:
                self.store.transition(incident["id"], "acknowledge", self.director)
                return "accepted"
            except DomainError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(results.count("accepted"), 1)
        self.assertEqual(len(self.store.incident(incident["id"])["events"]), 2)

    def test_concurrent_camera_creation_and_enable_respect_capacity(self):
        def create(n):
            try:
                return self.store.create_camera(
                    "Test " + str(n), "Lab", "test", "DORMGUARD_CAMERA_TEST", self.director, max_cameras=2
                )
            except DomainError as error:
                self.assertEqual(error.status, 409)
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(create, range(8)))
        self.assertEqual(sum(item is not None for item in results), 1)
        second = next(item for item in results if item is not None)
        self.store.delete_camera(second["id"], self.director)
        third = self.store.create_camera(
            "Third", "Lab", "test", "DORMGUARD_CAMERA_TEST", self.director, max_cameras=2
        )
        with self.assertRaises(DomainError):
            self.store.update_camera(second["id"], {"enabled": True}, self.director, max_cameras=2)
        self.store.delete_camera(third["id"], self.director)
        self.assertTrue(
            self.store.update_camera(second["id"], {"enabled": True}, self.director, max_cameras=2)["enabled"]
        )

    def test_role_and_disabled_guards(self):
        incident = self.incident()
        for actor in (self.admin, "missing"):
            with self.assertRaises(DomainError) as denied:
                self.store.transition(incident["id"], "acknowledge", actor)
            self.assertEqual(denied.exception.status, 403)
        for actor in (self.deputy, self.admin):
            with self.assertRaises(DomainError):
                self.store.create_camera("Denied", "Lab", "test", "test.mp4", actor)
        with self.store.db.transaction() as conn:
            conn.execute("UPDATE users SET enabled=0 WHERE id=?", (self.director,))
        with self.assertRaises(DomainError):
            self.store.add_note(incident["id"], self.director, "Denied")

    def test_transaction_rolls_back_incident_event_and_audit(self):
        before = self.store.verify_audit()["count"]
        with patch.object(self.store, "_audit", side_effect=RuntimeError("Controlled SQL failure")):
            with self.assertRaises(RuntimeError):
                self.incident()
        with self.store.db.read() as conn:
            for table in ("incidents", "events", "notification_outbox", "incident_keys"):
                self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM " + table).fetchone()["n"], 0)
        self.assertEqual(self.store.verify_audit()["count"], before)

    def test_evidence_original_tamper_missing_and_append_only(self):
        original = self.root / "controlled-fixture.mp4"
        original.write_bytes(b"Controlled byte fixture; no video/model claim")
        incident = self.incident(evidence_source=original, clip_start=0, clip_end=5)
        item = incident["evidence"][0]
        private = self.store.evidence(item["id"], True)
        path = self.store.evidence_path(private)
        self.assertEqual(path.read_bytes(), original.read_bytes())
        self.assertNotIn("path", item)
        self.assertEqual(path.stat().st_mode & 0o777, 0o400)
        self.assertTrue(self.store.verify_evidence(item["id"], self.director)["valid"])
        with self.assertRaises(DomainError):
            self.store.verify_evidence(item["id"], self.admin)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db.transaction() as conn:
                conn.execute("UPDATE evidence SET sha256='changed' WHERE id=?", (item["id"],))
        os.chmod(path, 0o600)
        path.write_bytes(b"tampered")
        self.assertEqual(self.store.verify_evidence(item["id"], self.director)["status"], "mismatch")
        path.unlink()
        self.assertEqual(self.store.verify_evidence(item["id"], self.director)["status"], "missing")
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_audit_chain_frozen_clock_and_database_trigger(self):
        with patch("dormguard.store.utcnow", return_value="2020-01-01T00:00:00+00:00"):
            self.store.audit(None, "test.one", "test")
            self.store.audit(None, "test.two", "test")
        self.assertTrue(self.store.verify_audit()["valid"])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db.transaction() as conn:
                conn.execute("DELETE FROM audit_logs")
        with self.store.db.transaction() as conn:
            conn.execute("DROP TRIGGER audit_logs_update")
            conn.execute(
                "UPDATE audit_logs SET action='tampered' WHERE id=(SELECT id FROM audit_logs LIMIT 1)"
            )
        self.assertFalse(self.store.verify_audit()["valid"])

    def test_full_history_aggregates_and_truthful_health(self):
        now = time.time()
        for n in range(205):
            self.incident(event_key="aggregate-" + str(n), cooldown_seconds=0, detected_at=now - n * 86400)
        overview = self.store.overview()
        self.assertEqual(overview["total_incidents"], 205)
        self.assertEqual(sum(row["count"] for row in overview["trends"]), 205)
        self.assertEqual(overview["connected_cameras"], 0)
        self.store.set_camera_health(self.camera, "connected", now)
        self.assertEqual(self.store.overview()["connected_cameras"], 1)
        self.store.set_camera_health(self.camera, "connected", now - 60)
        self.assertEqual(self.store.overview()["connected_cameras"], 0)
        self.store.heartbeat("test-worker", "blocked", "No checkpoint configured", {"queue": 0})
        self.assertEqual(self.store.health()[0]["state"], "blocked")

    def test_validation_and_path_escape(self):
        for score in (float("nan"), float("inf"), -1, 2):
            with self.assertRaises(DomainError):
                self.store.create_incident(self.camera, score, "controlled-test-model")
        with self.assertRaises(DomainError):
            self.incident(detected_at="2020-01-01T00:00:00")
        for timestamp in ("not-a-timestamp", float("nan"), float("inf")):
            with self.assertRaises(DomainError):
                self.incident(detected_at=timestamp)
        with self.assertRaises(DomainError):
            self.store.evidence_path({"path": "../outside.mp4"})
        with self.assertRaises(DomainError):
            self.store.provision_user("duplicate@example.test", "director")


if __name__ == "__main__":
    unittest.main()
