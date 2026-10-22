"""Real disposable HTTP/SQL denied-access audit; no hosted identity or messages."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dormguard.app import create_app
from dormguard.config import Settings
from dormguard.security import hash_password
from dormguard.store import Store


class DenialAuditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name))
        self.addCleanup(self.store.db.close)
        self.password = "CONTROLLED-password-for-audit-42"
        self.user = self.store.provision_user(
            "director@example.invalid", "director", hash_password(self.password)
        )
        self.client = TestClient(
            create_app(
                Settings(Path(directory.name), auth_mode="local", public_url="http://testserver"), self.store
            )
        )
        self.addCleanup(self.client.close)

    def test_wrong_password_is_audited_without_secret_or_email_disclosure(self):
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": self.user["email"], "password": "CONTROLLED-wrong-password"},
        )
        self.assertEqual(response.status_code, 401)
        row = next(row for row in self.store.audits() if row["action"] == "auth.denied")
        self.assertIsNone(row["actor_id"])
        self.assertEqual(row["resource_id"], "/api/v1/auth/login")
        self.assertEqual(row["detail"]["status"], 401)
        encoded = json.dumps(row)
        self.assertNotIn(self.user["email"], encoded)
        self.assertNotIn("password", encoded.lower())
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_trusted_session_role_denial_has_actor_and_route_without_query_secret(self):
        response = self.client.post(
            "/api/v1/auth/login", json={"email": self.user["email"], "password": self.password}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/users?private=CONTROLLED-QUERY-SECRET").status_code, 403)
        row = next(row for row in self.store.audits() if row["action"] == "authorization.denied")
        self.assertEqual(row["actor_id"], self.user["id"])
        self.assertEqual(row["resource_id"], "/api/v1/users")
        self.assertNotIn("CONTROLLED-QUERY-SECRET", json.dumps(row))

    def test_audit_failure_keeps_access_denied_and_emits_sanitized_diagnostic(self):
        with patch.object(self.store, "audit", side_effect=OSError("CONTROLLED-SECRET-ERROR")):
            with self.assertLogs("dormguard.app", "WARNING") as captured:
                result = self.client.get("/api/v1/incidents")
        self.assertEqual(result.status_code, 401)
        self.assertIn("security_audit_unavailable", "".join(captured.output))
        self.assertNotIn("CONTROLLED-SECRET-ERROR", "".join(captured.output))


if __name__ == "__main__":
    unittest.main()
