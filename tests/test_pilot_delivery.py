"""Disposable delivery regressions; every external response is a controlled double."""

import io
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from dormguard.config import Settings
from dormguard.notifications import TelegramWorker
from dormguard.store import Store
from dormguard.transport import RemoteError, request_json


class PilotDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dormguard-pilot-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        director = self.store.provision_user("director@example.invalid", "director")
        deputy = self.store.provision_user("deputy@example.invalid", "deputy_director")
        camera = self.store.create_camera("Camera 03", "Lab", "test", "TEST", director["id"])
        self.incident = self.store.create_incident(camera["id"], 0.8, "CONTROLLED MODEL DOUBLE")
        self.settings = Settings(Path(self.temp.name), telegram_bot_token="CONTROLLED_TEST_TOKEN")
        with self.store.db.transaction() as conn:
            for user, chat in ((director, "101"), (deputy, "102")):
                conn.execute("INSERT INTO telegram_bindings VALUES(?,?,?)", (user["id"], chat, "test"))

    def test_transport_preserves_telegram_retry_minimum_over_an_hour(self):
        error = HTTPError(
            "https://provider.invalid", 429, "test", {}, io.BytesIO(b'{"parameters":{"retry_after":7200}}')
        )
        with patch("dormguard.transport.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(RemoteError) as raised:
                request_json("POST", "https://provider.invalid")
        self.assertEqual(raised.exception.retry_after, 7200)

    def test_json_api_error_is_classified_and_jitter_never_shortens_retry_minimum(self):
        worker = TelegramWorker(
            self.store,
            self.settings,
            lambda *a, **k: {"ok": False, "error_code": 429, "parameters": {"retry_after": 7200}},
        )
        before = time.time()
        worker.run_once()
        with self.store.db.read() as conn:
            row = conn.execute("SELECT * FROM notification_outbox WHERE attempts=1").fetchone()
        self.assertEqual(row["last_error"], "provider_429")
        self.assertGreaterEqual(row["next_attempt"], before + 7200)
        self.assertLess(row["next_attempt"], before + 7212)

    def test_concurrent_workers_send_each_recipient_once_and_restart_retains_state(self):
        calls = []

        def controlled_transport(*args, **kwargs):
            calls.append(kwargs["body"])
            time.sleep(0.01)
            return {"ok": True, "result": {"message_id": len(calls)}}

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda _: TelegramWorker(self.store, self.settings, controlled_transport).run_once(),
                    range(8),
                )
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual({row["chat_id"] for row in calls}, {"101", "102"})
        self.assertIn("Camera 03", calls[0]["text"])
        self.assertIn("DETECTED", calls[0]["text"])
        reopened = Store(Path(self.temp.name))
        self.assertFalse(TelegramWorker(reopened, self.settings, controlled_transport).run_once())
        self.assertTrue(reopened.verify_audit()["valid"])

    def test_scoped_opt_in_claim_cannot_send_other_pending_alerts(self):
        with self.store.db.read() as conn:
            chosen = conn.execute("SELECT id FROM notification_outbox ORDER BY id LIMIT 1").fetchone()["id"]
        calls = []
        worker = TelegramWorker(
            self.store,
            self.settings,
            lambda *a, **k: calls.append(k["body"]) or {"ok": True, "result": {"message_id": 1}},
        )
        self.assertFalse(worker.run_once(outbox_id="not-present"))
        self.assertTrue(worker.run_once(outbox_id=chosen))
        self.assertFalse(worker.run_once(outbox_id=chosen))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.operational_summary()["outbox_counts"], {"accepted": 1, "queued": 1})


if __name__ == "__main__":
    unittest.main()
