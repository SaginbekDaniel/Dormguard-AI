"""Actual disposable SQL/HTTP account tests; hosted identity uses a labeled contract double."""

import hashlib
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from test_security_and_delivery import ControlledAuth

from dormguard.app import COOKIE, create_app
from dormguard.config import Settings
from dormguard.security import hash_password
from dormguard.store import DomainError, Store

PASSWORD = "controlled-final-account-password"


class FinalAccountTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dormguard-final-accounts-")
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(Path(temporary.name), auth_mode="local", public_url="http://testserver")
        self.store = Store(self.settings.data_dir)
        password_hash = hash_password(PASSWORD)
        self.director = self.store.provision_user("director@example.invalid", "director", password_hash)
        self.deputy = self.store.provision_user("deputy@example.invalid", "deputy_director", password_hash)
        self.admin = self.store.provision_user("super@example.invalid", "super_admin", password_hash)
        self.technical = self.store.provision_user("tech@example.invalid", "technical_admin", password_hash)
        self.client = TestClient(create_app(self.settings, self.store))
        self.addCleanup(self.client.close)

    def login(self, user, client=None):
        client = client or self.client
        response = client.post("/api/v1/auth/login", json={"email": user["email"], "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"X-CSRF-Token": response.json()["csrf_token"]}

    def test_super_admin_can_review_and_manage_but_technical_admin_is_denied(self):
        headers = self.login(self.admin)
        response = self.client.post(
            "/api/v1/cameras",
            headers=headers,
            json={
                "name": "Generated test",
                "location": "Disposable",
                "kind": "test",
                "source_ref": "DORMGUARD_CAMERA_TEST",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        incident = self.store.create_incident(
            response.json()["id"], 0.8, "CONTROLLED FIXTURE", event_key="one"
        )
        self.assertEqual(self.client.get("/api/v1/incidents/" + incident["id"]).status_code, 200)
        deliveries = self.client.get("/api/v1/incidents/" + incident["id"]).json()["notifications"]
        self.assertEqual(len(deliveries), 2)
        self.assertTrue(all(not delivery["can_retry"] for delivery in deliveries))
        self.assertNotIn("chat_id", str(deliveries))
        self.login(self.director)
        self.assertEqual(
            len(self.client.get("/api/v1/incidents/" + incident["id"]).json()["notifications"]), 1
        )
        headers = self.login(self.admin)
        self.assertEqual(
            self.client.post(
                "/api/v1/incidents/" + incident["id"] + "/acknowledge", headers=headers, json={}
            ).status_code,
            200,
        )
        users = self.client.get("/api/v1/users")
        self.assertEqual(users.status_code, 200)
        self.assertNotIn("password_hash", users.text)
        self.assertNotIn("provider_token", users.text)
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/login", json={"email": self.technical["email"], "password": PASSWORD}
            ).status_code,
            401,
        )
        # Failed login does not promote the technical identity or issue its session.
        with self.store.db.read() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions WHERE user_id=?", (self.technical["id"],)
                ).fetchone()["n"],
                0,
            )

    def test_own_profile_is_persistent_audited_and_cannot_change_identity_or_role(self):
        self.assertEqual(
            self.client.patch("/api/v1/auth/profile", json={"display_name": "A"}).status_code, 401
        )
        headers = self.login(self.director)
        self.assertEqual(
            self.client.patch("/api/v1/auth/profile", json={"display_name": "Director"}).status_code, 403
        )
        for body in (
            {"display_name": ""},
            {"display_name": "  "},
            {"display_name": "Bad\nName"},
            {"display_name": "A" * 81},
            {"display_name": "A", "role": "super_admin"},
            {"display_name": "A", "user_id": self.deputy["id"]},
        ):
            self.assertEqual(
                self.client.patch("/api/v1/auth/profile", headers=headers, json=body).status_code, 422
            )
        self.assertEqual(
            self.client.patch(
                "/api/v1/auth/profile", headers=headers, json={"display_name": "  Alex <reviewer>  "}
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/v1/auth/me").json()["display_name"], "Alex <reviewer>")
        self.assertEqual(
            Store(self.settings.data_dir).user(self.director["id"])["display_name"], "Alex <reviewer>"
        )
        self.assertEqual(self.store.user(self.director["id"])["role"], "director")
        self.assertEqual(self.store.user(self.deputy["id"])["display_name"], "")
        self.assertEqual(self.store.audit_page(q="profile.updated")["total"], 1)
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_availability_requires_super_admin_and_revokes_every_session_and_ticket(self):
        headers = self.login(self.director)
        cookie = self.client.cookies.get(COOKIE)
        camera = self.store.create_camera(
            "Fixture", "Lab", "test", "DORMGUARD_CAMERA_TEST", self.director["id"]
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/cameras/{camera['id']}/stream-session", headers=headers, json={}
            ).status_code,
            200,
        )
        for user in (self.director, self.deputy):
            official_headers = self.login(user)
            self.assertEqual(self.client.get("/api/v1/users").status_code, 403)
            self.assertEqual(
                self.client.patch(
                    "/api/v1/users/" + self.director["id"], headers=official_headers, json={"enabled": False}
                ).status_code,
                403,
            )
        headers = self.login(self.admin)
        path = "/api/v1/users/" + self.director["id"]
        self.assertEqual(self.client.patch(path, json={"enabled": False}).status_code, 403)
        self.assertEqual(
            self.client.patch(
                path, headers=headers, json={"enabled": False, "role": "super_admin"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.patch(
                "/api/v1/users/" + self.admin["id"], headers=headers, json={"enabled": False}
            ).status_code,
            409,
        )
        self.assertEqual(self.client.patch(path, headers=headers, json={"enabled": False}).status_code, 200)
        with TestClient(create_app(self.settings, self.store)) as revoked:
            revoked.cookies.set(COOKIE, cookie)
            self.assertEqual(revoked.get("/api/v1/incidents").status_code, 401)
        with self.store.db.read() as conn:
            for table in ("sessions", "stream_tickets"):
                self.assertEqual(
                    conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table} WHERE user_id=?", (self.director["id"],)
                    ).fetchone()["n"],
                    0,
                )
        self.assertEqual(self.client.patch(path, headers=headers, json={"enabled": True}).status_code, 200)
        self.assertTrue(self.store.verify_audit()["valid"])

    def test_provider_metadata_never_promotes_and_outage_denies_new_sensitive_writes(self):
        provider = ControlledAuth(self.deputy)
        with TestClient(
            create_app(replace(self.settings, auth_mode="supabase"), self.store, provider)
        ) as client:
            headers = self.login(self.deputy, client)
            self.assertEqual(client.get("/api/v1/users").status_code, 403)
            provider.user = self.admin
            self.assertEqual(
                client.patch(
                    "/api/v1/auth/profile", headers=headers, json={"display_name": "Denied"}
                ).status_code,
                401,
            )
        for role in ("member", "student", "anonymous", "unknown"):
            with self.assertRaises(DomainError):
                self.store.provision_user(role + "@example.invalid", role)


class AccountUpgradeTests(unittest.TestCase):
    def test_customized_schema_refused_without_losing_custom_data(self):
        with tempfile.TemporaryDirectory(prefix="dormguard-custom-upgrade-") as directory:
            path = Path(directory)
            conn = sqlite3.connect(path / "dormguard.sqlite3")
            conn.executescript(
                (Path(__file__).resolve().parents[1] / "migrations/001_schema.sql").read_text()
            )
            conn.execute("ALTER TABLE users ADD COLUMN private_custom_field TEXT")
            conn.execute(
                "INSERT INTO users VALUES('old','old@example.invalid','director',NULL,1,'original','preserve')"
            )
            conn.commit()
            conn.close()
            with self.assertRaisesRegex(ValueError, "customized users schema"):
                Store(path)
            with sqlite3.connect(path / "dormguard.sqlite3") as conn:
                self.assertEqual(
                    conn.execute("SELECT private_custom_field FROM users").fetchone()[0], "preserve"
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=3").fetchone()[0], 0
                )

    def test_v2_upgrade_preserves_users_sessions_tickets_and_foreign_keys(self):
        with tempfile.TemporaryDirectory(prefix="dormguard-v2-upgrade-") as directory:
            path = Path(directory)
            conn = sqlite3.connect(path / "dormguard.sqlite3")
            schema = Path(__file__).resolve().parents[1] / "migrations/001_schema.sql"
            conn.executescript(schema.read_text())
            conn.row_factory = sqlite3.Row
            conn.execute("INSERT INTO users VALUES('old','old@example.invalid','director',NULL,1,'original')")
            conn.execute(
                "INSERT INTO cameras VALUES('cam','Old camera','Lab','test','DORMGUARD_CAMERA_TEST',1,'original')"
            )
            conn.execute("INSERT INTO sessions VALUES('session','old','csrf',?,NULL)", (time.time() + 3600,))
            conn.execute(
                "INSERT INTO stream_tickets VALUES('ticket','old','cam','session',?)", (time.time() + 30,)
            )
            conn.execute(
                "INSERT INTO incidents(id,camera_id,status,score,model,detected_at,detected_epoch,event_key,created_at,updated_at) VALUES('legacy-incident','cam','DETECTED',0.0,'CONTROLLED LEGACY FIXTURE','original',0,'legacy-event','original','original')"
            )
            originals = path / "evidence"
            originals.mkdir(mode=0o700)
            original = originals / "legacy-original.mp4"
            original.write_bytes(b"Controlled preservation fixture; not actual video")
            original.chmod(0o400)
            original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
            conn.execute(
                "INSERT INTO evidence VALUES('legacy-evidence','legacy-incident','legacy-original.mp4','legacy-original.mp4',?, ?,0,1,'original')",
                (original_hash, original.stat().st_size),
            )
            # Only use the audit append helper to seed the legacy SQL fixture;
            # do not initialize/migrate Store before the v2 state is complete.
            Store._audit(object.__new__(Store), conn, "old", "controlled.legacy.fixture", "legacy-incident")
            audit_before = [dict(row) for row in conn.execute("SELECT * FROM audit_logs").fetchall()]
            conn.commit()
            conn.close()
            for _ in range(2):
                store = Store(path)
                self.assertEqual(store.user("old")["created_at"], "original")
                self.assertEqual(store.user("old")["display_name"], "")
                self.assertTrue(store.verify_audit()["valid"])
                self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), original_hash)
                with store.db.read() as db:
                    self.assertEqual(
                        [dict(row) for row in db.execute("SELECT * FROM audit_logs").fetchall()], audit_before
                    )
                    self.assertEqual(
                        db.execute("SELECT sha256 FROM evidence WHERE id='legacy-evidence'").fetchone()[
                            "sha256"
                        ],
                        original_hash,
                    )
                    self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"], 1)
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) AS n FROM stream_tickets").fetchone()["n"], 1
                    )
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                store.provision_user("super@example.invalid", "super_admin")["role"], "super_admin"
            )
