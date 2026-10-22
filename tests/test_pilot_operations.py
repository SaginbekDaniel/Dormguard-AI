"""Non-destructive integrity, retention inspection, metrics and API authorization."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from dormguard.app import create_app
from dormguard.config import Settings
from dormguard.maintenance import report
from dormguard.store import Store
from dormguard.telemetry import Metrics


class OperationsTests(unittest.TestCase):
    def test_retention_report_excludes_active_and_detects_missing_tampered_orphans_without_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            director = store.provision_user("director@example.invalid", "director")["id"]
            camera = store.create_camera("Generated", "Lab", "test", "TEST", director)["id"]
            source = Path(directory) / "original.bin"
            source.write_bytes(b"CONTROLLED NONVIDEO INTEGRITY FIXTURE")
            active = store.create_incident(camera, 0.8, "CONTROLLED TEST", cooldown_seconds=0)
            closed = store.create_incident(camera, 0.8, "CONTROLLED TEST", cooldown_seconds=0)
            store.transition(closed["id"], "false-positive", director, "Controlled test")
            active_ev = store.add_evidence(active["id"], source, 0, 1)
            missing_ev = store.add_evidence(closed["id"], source, 0, 1)
            mismatched_ev = store.add_evidence(closed["id"], source, 0, 1)
            store.evidence_path(store.evidence(missing_ev["id"], True)).unlink()
            altered = store.evidence_path(store.evidence(mismatched_ev["id"], True))
            altered.chmod(0o600)
            altered.write_bytes(b"CONTROLLED TAMPERED BY PRIVILEGED TEST")
            orphan = store.evidence_dir / "unclassified.bin"
            orphan.write_bytes(b"CONTROLLED UNKNOWN FILE")
            before = sorted(p.name for p in store.evidence_dir.iterdir())
            result = report(store, 30)
            self.assertEqual(result["missing_evidence_ids"], [missing_ev["id"]])
            self.assertEqual(result["mismatched_evidence_ids"], [mismatched_ev["id"]])
            self.assertNotIn(active_ev["id"], result["candidate_evidence_ids"])
            self.assertEqual(result["unclassified_file_count"], 1)
            self.assertEqual(result["eligible_for_automatic_deletion"], [])
            self.assertEqual(before, sorted(p.name for p in store.evidence_dir.iterdir()))
            self.assertTrue(store.verify_audit()["valid"])

    def test_metrics_are_bounded_and_system_requires_authorized_session(self):
        metrics = Metrics(capacity=20)
        for value in range(100):
            metrics.observe("inference_seconds", value / 100)
        result = metrics.snapshot()["series"]["inference_seconds"]
        self.assertEqual(result["count"], 20)
        self.assertEqual(result["total_observations"], 100)
        self.assertIsNone(result["p99"])
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            with TestClient(create_app(Settings(Path(directory)), store)) as client:
                self.assertEqual(client.get("/api/v1/system").status_code, 401)
            self.assertEqual(store.operational_summary()["outbox_counts"], {})

    def test_recording_client_open_requires_official_and_does_not_acknowledge(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            director = store.provision_user("director@example.invalid", "director")["id"]
            admin = store.provision_user("admin@example.invalid", "technical_admin")["id"]
            camera = store.create_camera("Generated", "Lab", "test", "TEST", director)["id"]
            incident = store.create_incident(camera, 0.8, "CONTROLLED TEST")
            from dormguard.store import DomainError

            with self.assertRaises(DomainError):
                store.record_incident_opened(incident["id"], admin)
            self.assertTrue(store.record_incident_opened(incident["id"], director)["recorded"])
            self.assertEqual(store.incident(incident["id"])["status"], "DETECTED")
            access = next(a for a in store.audits() if a["action"] == "incident.opened")
            self.assertEqual(access["detail"]["correlation_id"], incident["id"])
