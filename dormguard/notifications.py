"""Persistent leased Telegram delivery, bounded retries, no external exactly-once claim."""

import argparse
import hmac
import re
import secrets
import signal
import threading
import time
import uuid
from datetime import datetime

from .config import load_settings
from .security import OFFICIAL_ROLES, token_hash
from .store import DomainError, Store, utcnow
from .transport import RemoteError, request_json, retry_seconds


def create_link(store, user_id, settings):
    if (
        not settings.telegram_bot_token
        or not settings.telegram_webhook_secret
        or not re.fullmatch(r"[A-Za-z0-9_]{5,64}", settings.telegram_bot_username)
    ):
        raise DomainError("Telegram bot and secret webhook are not configured", 503)
    token = secrets.token_urlsafe(24)
    expires = time.time() + 300
    with store.db.transaction() as conn:
        store._actor(conn, user_id)
        conn.execute("DELETE FROM telegram_links WHERE user_id=? OR expires_at<?", (user_id, time.time()))
        conn.execute(
            "INSERT INTO telegram_links(token_hash,user_id,expires_at) VALUES(?,?,?)",
            (token_hash(token), user_id, expires),
        )
        store._audit(conn, user_id, "telegram.link_requested", user_id)
    return {
        "token": token,
        "expires_at": expires,
        "url": f"https://t.me/{settings.telegram_bot_username}?start={token}",
    }


def bind_webhook(store, settings, supplied_secret, payload):
    if not settings.telegram_webhook_secret or not hmac.compare_digest(
        settings.telegram_webhook_secret, supplied_secret or ""
    ):
        raise DomainError("Webhook authentication failed", 403)
    message = payload.get("message", {})
    if not isinstance(message, dict):
        raise DomainError("Invalid webhook message", 422)
    chat = message.get("chat", {})
    sender = message.get("from", {})
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        raise DomainError("Invalid webhook account", 422)
    text = message.get("text", "")
    if chat.get("type") != "private" or not isinstance(text, str) or not text.startswith("/start "):
        return {"ok": True, "linked": False}
    token = text.split(" ", 1)[1].strip()
    chat_id = str(chat.get("id", ""))
    sender_id = str(sender.get("id", ""))
    if not re.fullmatch(r"[1-9][0-9]{0,19}", chat_id) or sender_id != chat_id or not 20 <= len(token) <= 64:
        raise DomainError("Private account binding required", 422)
    with store.db.transaction() as conn:
        link = conn.execute(
            "SELECT * FROM telegram_links WHERE token_hash=? AND expires_at>?",
            (token_hash(token), time.time()),
        ).fetchone()
        if not link:
            raise DomainError("Link token expired or already used", 409)
        store._actor(conn, link["user_id"])
        existing = conn.execute(
            "SELECT user_id FROM telegram_bindings WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if existing and existing["user_id"] != link["user_id"]:
            raise DomainError("This private chat is already assigned", 409)
        conn.execute(
            "INSERT INTO telegram_bindings(user_id,chat_id,created_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,created_at=excluded.created_at",
            (link["user_id"], chat_id, utcnow()),
        )
        conn.execute("DELETE FROM telegram_links WHERE user_id=?", (link["user_id"],))
        conn.execute(
            "UPDATE notification_outbox SET state='queued',next_attempt=?,last_error=NULL WHERE user_id=? AND state='blocked'",
            (time.time(), link["user_id"]),
        )
        store._audit(conn, link["user_id"], "telegram.bound", link["user_id"])
    return {"ok": True, "linked": True}


def unlink(store, user_id):
    with store.db.transaction() as conn:
        store._actor(conn, user_id)
        conn.execute("DELETE FROM telegram_bindings WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM telegram_links WHERE user_id=?", (user_id,))
        conn.execute(
            "UPDATE notification_outbox SET state='cancelled',last_error='recipient_unlinked',lease_token=NULL,lease_until=NULL WHERE user_id=? AND state IN ('queued','blocked','failed','sending')",
            (user_id,),
        )
        store._audit(conn, user_id, "telegram.unlinked", user_id)


def send_account_test(store, user_id, settings, transport=None):
    """Explicit own-account test. No incident, outbox row, or automatic retry is fabricated."""
    if not settings.telegram_bot_token or not settings.telegram_webhook_secret:
        raise DomainError("Telegram is not configured", 503)
    transport = transport or request_json
    with store.db.transaction() as conn:
        store._actor(conn, user_id)
        binding = store._one(conn, "SELECT chat_id FROM telegram_bindings WHERE user_id=?", (user_id,))
        if not binding:
            raise DomainError("Connect your own private Telegram account first", 409)
        store._audit(conn, user_id, "telegram.test_requested", user_id)
    try:
        result = transport(
            "POST",
            "https://api.telegram.org/bot" + settings.telegram_bot_token + "/sendMessage",
            body={
                "chat_id": binding["chat_id"],
                "text": "DormGuard AI — account connection test. This is a test requested from your account settings, not an incident alert.",
                "link_preview_options": {"is_disabled": True},
            },
        )
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or not isinstance(result.get("result"), dict)
            or type(result["result"].get("message_id")) is not int
            or result["result"]["message_id"] <= 0
        ):
            raise RemoteError(502)
    except RemoteError as error:
        store.audit(user_id, "telegram.test_failed", user_id, {"provider_status": error.status})
        raise DomainError(
            "Telegram did not confirm API acceptance. Check your connection and try again later.", 503
        ) from None
    store.audit(user_id, "telegram.test_accepted", user_id, {"api_acceptance_is_device_delivery": False})
    return {"accepted": True, "device_delivery": "unverified", "accepted_at": utcnow()}


class TelegramWorker:
    def __init__(self, store, settings, transport=request_json):
        self.store, self.settings, self.transport = store, settings, transport

    def _claim(self, outbox_id=None):
        now = time.time()
        lease = secrets.token_hex(16)
        with self.store.db.transaction() as conn:
            scope = "id=? AND " if outbox_id else ""
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE "
                + scope
                + "((state='queued' AND next_attempt<=?) OR (state='sending' AND lease_until<?)) ORDER BY created_at,id LIMIT 1",
                (outbox_id, now, now) if outbox_id else (now, now),
            ).fetchone()
            if not row:
                return None
            row = dict(row)
            conn.execute(
                "UPDATE notification_outbox SET state='sending',lease_until=?,lease_token=?,attempts=attempts+1 WHERE id=?",
                (now + 60, lease, row["id"]),
            )
            row.update(lease_token=lease, attempts=row["attempts"] + 1)
            return row

    def _finish(self, row, state, detail, provider_id=None, retry_after=0):
        with self.store.db.transaction() as conn:
            owned = conn.execute(
                "SELECT id FROM notification_outbox WHERE id=? AND lease_token=? AND state='sending'",
                (row["id"], row["lease_token"]),
            ).fetchone()
            if not owned:
                return
            conn.execute(
                "UPDATE notification_outbox SET state=?,last_error=?,provider_id=?,next_attempt=?,lease_until=NULL,lease_token=NULL WHERE id=?",
                (state, detail, provider_id, time.time() + retry_after, row["id"]),
            )
            conn.execute(
                "INSERT INTO notification_attempts(id,outbox_id,state,detail,created_at) VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), row["id"], state, detail or "provider_accepted", utcnow()),
            )
            self.store._audit(
                conn,
                None,
                "notification." + state,
                row["id"],
                {
                    "attempt": row["attempts"],
                    "detail": detail,
                    "correlation_id": row["incident_id"],
                    "timing": row.get("timing", {}),
                },
            )

    def run_once(self, outbox_id=None):
        row = self._claim(outbox_id)
        if row is None:
            return False
        if row["attempts"] > 6:
            self._finish(row, "failed", "attempt_limit_after_lease_recovery")
            return True
        with self.store.db.read() as conn:
            recipient = conn.execute(
                "SELECT u.enabled,u.role,b.chat_id FROM users u LEFT JOIN telegram_bindings b ON b.user_id=u.id WHERE u.id=?",
                (row["user_id"],),
            ).fetchone()
        if (
            not recipient
            or not recipient["enabled"]
            or recipient["role"] not in OFFICIAL_ROLES
            or not recipient["chat_id"]
        ):
            self._finish(row, "blocked", "recipient_unavailable")
            return True
        if not self.settings.telegram_bot_token:
            self._finish(row, "blocked", "telegram_not_configured")
            return True
        incident = self.store.incident(row["incident_id"])
        if not incident:
            self._finish(row, "failed", "incident_missing")
            return True
        # Plain text avoids HTML/Markdown injection. Links open the authenticated dashboard.
        text = (
            "DormGuard AI — suspected physical altercation\n"
            f"Camera: {incident.get('camera_name', incident['camera_id'])}\n"
            f"Location: {incident.get('camera_location', incident.get('location', ''))}\n"
            f"Detected: {incident['detected_at']}\n"
            "Severity: requires human review\n"
            f"Incident: {incident['id']}\n"
            f"Status: {incident['status']}\n"
            f"Review: {self.settings.public_url}/#incidents/{incident['id']}\n"
            f"Protected camera preview: {self.settings.public_url}/#cameras/{incident['camera_id']}"
        )
        try:
            # Check eligibility before recording the dispatch attempt. A claim
            # alone does not authorize a recipient or an external request.
            with self.store.db.read() as conn:
                current = conn.execute(
                    "SELECT o.state,o.lease_token,u.enabled,u.role,b.chat_id FROM notification_outbox o JOIN users u ON u.id=o.user_id LEFT JOIN telegram_bindings b ON b.user_id=u.id WHERE o.id=?",
                    (row["id"],),
                ).fetchone()
            if not current or current["state"] != "sending" or current["lease_token"] != row["lease_token"]:
                return True
            if not current["enabled"] or current["role"] not in OFFICIAL_ROLES or not current["chat_id"]:
                self._finish(row, "blocked", "recipient_unavailable")
                return True
            row["timing"] = {
                "T8": utcnow(),
                "clock": "UTC wall time + process monotonic request duration",
                "clock_sync_uncertainty_seconds": None,
            }
            with self.store.db.transaction() as conn:
                self.store._audit(
                    conn,
                    None,
                    "notification.request_started",
                    row["id"],
                    {
                        "correlation_id": row["incident_id"],
                        "attempt": row["attempts"],
                        "timing": row["timing"],
                    },
                )
            # The audit transaction can wait on remote SQL. Resolve once more
            # after it commits so cancellation/rebind/lease takeover completed
            # during that wait prevents an unstarted request to a stale chat.
            # The final check and external I/O cannot be one atomic operation.
            with self.store.db.read() as conn:
                current = conn.execute(
                    "SELECT o.state,o.lease_token,u.enabled,u.role,b.chat_id FROM notification_outbox o JOIN users u ON u.id=o.user_id LEFT JOIN telegram_bindings b ON b.user_id=u.id WHERE o.id=?",
                    (row["id"],),
                ).fetchone()
            if not current or current["state"] != "sending" or current["lease_token"] != row["lease_token"]:
                return True
            if not current["enabled"] or current["role"] not in OFFICIAL_ROLES or not current["chat_id"]:
                self._finish(row, "blocked", "recipient_unavailable")
                return True
            requested = time.monotonic()
            result = self.transport(
                "POST",
                "https://api.telegram.org/bot" + self.settings.telegram_bot_token + "/sendMessage",
                body={
                    "chat_id": current["chat_id"],
                    "text": text,
                    "link_preview_options": {"is_disabled": True},
                },
            )
            if isinstance(result, dict) and result.get("ok") is False:
                code = result.get("error_code")
                if type(code) is int and 400 <= code <= 599:
                    raise RemoteError(code, retry_seconds(result) if code == 429 else None)
            if (
                not isinstance(result, dict)
                or result.get("ok") is not True
                or not isinstance(result.get("result"), dict)
                or type(result["result"].get("message_id")) is not int
                or result["result"]["message_id"] <= 0
            ):
                raise RemoteError(502)
            accepted = utcnow()
            row["timing"].update(T9=accepted, request_seconds_monotonic=time.monotonic() - requested)
            elapsed = (
                datetime.fromisoformat(accepted).timestamp()
                - datetime.fromisoformat(incident["detected_at"]).timestamp()
            )
            queue_delay = (
                datetime.fromisoformat(row["timing"]["T8"]).timestamp()
                - datetime.fromisoformat(row["created_at"]).timestamp()
            )
            row["timing"].update(
                latest_sample_to_acceptance_seconds_wall_estimate=elapsed if elapsed >= 0 else None,
                queue_delay_seconds_wall_estimate=queue_delay if queue_delay >= 0 else None,
                api_acceptance_is_device_delivery=False,
            )
            self._finish(row, "accepted", None, str(result["result"]["message_id"]))
        except RemoteError as exc:
            row["timing"].update(
                request_failed_utc=utcnow(),
                request_seconds_monotonic=time.monotonic() - requested,
                provider_status=exc.status,
            )
            transient = exc.status == 429 or exc.status >= 500
            state = "queued" if transient and row["attempts"] < 6 else "failed"
            base = min(3600, 5 * 2 ** min(row["attempts"], 10))
            # Positive bounded jitter spreads retries without violating the
            # provider's mandatory minimum, including waits longer than one hour.
            wait = max(exc.retry_after or 0, base) + secrets.randbelow(1001) / 1000 * min(10, base / 4)
            self._finish(row, state, "provider_" + str(exc.status), retry_after=wait)
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    store = Store(settings.data_dir, settings.database_url)
    worker = TelegramWorker(store, settings)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            store.heartbeat(
                "notifications",
                "ready" if settings.telegram_bot_token else "blocked",
                "" if settings.telegram_bot_token else "Telegram not configured",
            )
            processed = worker.run_once()
            if args.once:
                break
            if not processed:
                stop.wait(1)
    finally:
        store.heartbeat("notifications", "stopped")


if __name__ == "__main__":
    main()
