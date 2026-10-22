"""Cancel/rebind during SQL audit; transport is an explicit no-network double."""

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from dormguard import notifications
from dormguard.config import Settings
from dormguard.store import Store


class NotificationCancellationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="dormguard-notification-cancel-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name))
        self.addCleanup(self.store.db.close)
        self.user = self.store.provision_user("director@example.invalid", "director")["id"]
        self.settings = Settings(
            Path(self.directory.name), auth_mode="local", telegram_bot_token="CONTROLLED-NO-NETWORK-DOUBLE"
        )
        camera = self.store.create_camera("Controlled", "Test", "test", "DORMGUARD_CAMERA_TEST", self.user)
        incident = self.store.create_incident(camera["id"], 0.8, "CONTROLLED_CANCELLATION_TEST_DOUBLE")
        with self.store.db.transaction() as conn:
            conn.execute("INSERT INTO telegram_bindings VALUES(?,?,?)", (self.user, "101", "controlled"))
            self.outbox = conn.execute(
                "SELECT id FROM notification_outbox WHERE incident_id=? AND user_id=?",
                (incident["id"], self.user),
            ).fetchone()["id"]

    def dispatch_after_audit(self, change):
        """Complete the control change after the request-start audit commits."""
        original_audit, original_transaction = self.store._audit, self.store.db.transaction
        started, changed, calls = [], [], []

        def audit(conn, actor, action, *args, **kwargs):
            original_audit(conn, actor, action, *args, **kwargs)
            if action == "notification.request_started":
                started.append(True)

        @contextmanager
        def transaction(*args, **kwargs):
            with original_transaction(*args, **kwargs) as conn:
                yield conn
            if started and not changed:
                changed.append(True)
                change()

        def controlled_transport(*args, **kwargs):
            self.assertTrue(changed, "Transport started before the controlled cancellation boundary")
            calls.append(kwargs["body"])
            return {"ok": True, "result": {"message_id": 1}}

        with (
            patch.object(self.store, "_audit", side_effect=audit),
            patch.object(self.store.db, "transaction", transaction),
        ):
            self.assertTrue(
                notifications.TelegramWorker(self.store, self.settings, controlled_transport).run_once(
                    self.outbox
                )
            )
        self.assertTrue(changed, "Controlled post-audit change was not executed")
        return calls

    def outbox_row(self):
        with self.store.db.read() as conn:
            return dict(
                conn.execute("SELECT * FROM notification_outbox WHERE id=?", (self.outbox,)).fetchone()
            )

    def test_unlink_committed_after_request_audit_prevents_unstarted_send(self):
        calls = self.dispatch_after_audit(lambda: notifications.unlink(self.store, self.user))
        self.assertEqual(calls, [], "Unlinked recipient reached an unstarted transport")
        self.assertEqual(self.outbox_row()["state"], "cancelled")

    def test_disable_committed_after_request_audit_blocks_unstarted_send(self):
        def disable():
            with self.store.db.transaction() as conn:
                conn.execute("UPDATE users SET enabled=0 WHERE id=?", (self.user,))

        calls = self.dispatch_after_audit(disable)
        self.assertEqual(calls, [], "Disabled recipient reached an unstarted transport")
        self.assertEqual(self.outbox_row()["state"], "blocked")

    def test_rebind_after_request_audit_uses_current_private_binding(self):
        def rebind():
            with self.store.db.transaction() as conn:
                conn.execute("UPDATE telegram_bindings SET chat_id='202' WHERE user_id=?", (self.user,))

        calls = self.dispatch_after_audit(rebind)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["chat_id"], "202")
        self.assertEqual(self.outbox_row()["state"], "accepted")

    def test_lease_takeover_after_request_audit_prevents_old_owner_send(self):
        def takeover():
            with self.store.db.transaction() as conn:
                conn.execute(
                    "UPDATE notification_outbox SET lease_token='CONTROLLED-NEW-OWNER' WHERE id=?",
                    (self.outbox,),
                )

        calls = self.dispatch_after_audit(takeover)
        self.assertEqual(calls, [], "Previous lease owner reached an unstarted transport")
        self.assertEqual(self.outbox_row()["state"], "sending")
        self.assertEqual(self.outbox_row()["lease_token"], "CONTROLLED-NEW-OWNER")


if __name__ == "__main__":
    unittest.main()
