"""Opt-in send to TWO explicitly selected, already bound official accounts.

Never run without separate authorization for the selected incident/recipients.
No automatic account binding, incident creation, retry reset or queue-wide sends.
Credentials come from operator environment; no .env is implicitly loaded.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-send", action="store_true")
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--director-id", required=True)
    parser.add_argument("--deputy-id", required=True)
    args = parser.parse_args()
    if not args.allow_send:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "separate_explicit_recipient_and_message_authorization_required",
                }
            )
        )
        return 2
    from dormguard.config import load_settings
    from dormguard.notifications import TelegramWorker
    from dormguard.store import Store
    from dormguard.transport import RemoteError, request_json

    settings = load_settings()
    if (
        settings.environment != "evaluation"
        or not settings.public_url.startswith("https://")
        or not settings.telegram_bot_token
    ):
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "requires_configured_evaluation_bot_and_HTTPS_incident_origin",
                }
            )
        )
        return 2
    store = Store(settings.data_dir, settings.database_url)
    if not store.incident(args.incident_id):
        print(json.dumps({"status": "BLOCKED", "reason": "selected_incident_missing"}))
        return 2
    targets = []
    with store.db.read() as conn:
        for role, user_id in (("director", args.director_id), ("deputy_director", args.deputy_id)):
            row = conn.execute(
                "SELECT o.id,o.state FROM notification_outbox o JOIN users u ON u.id=o.user_id JOIN telegram_bindings b ON b.user_id=u.id WHERE o.incident_id=? AND u.id=? AND u.role=? AND u.enabled=1",
                (args.incident_id, user_id, role),
            ).fetchone()
            if not row:
                print(
                    json.dumps(
                        {
                            "status": "BLOCKED",
                            "reason": "explicit_enabled_bound_role_and_existing_outbox_required",
                            "role": role,
                        }
                    )
                )
                return 2
            targets.append((role, row["id"]))
    try:
        auth = request_json("GET", "https://api.telegram.org/bot" + settings.telegram_bot_token + "/getMe")
        if (
            auth.get("ok") is not True
            or not isinstance(auth.get("result"), dict)
            or auth["result"].get("is_bot") is not True
        ):
            raise RemoteError(502)
        result = []
        worker = TelegramWorker(store, settings)
        for role, outbox_id in targets:
            before = time.monotonic()
            processed = worker.run_once(outbox_id=outbox_id)
            with store.db.read() as conn:
                row = conn.execute(
                    "SELECT state,attempts,last_error FROM notification_outbox WHERE id=?", (outbox_id,)
                ).fetchone()
            result.append(
                {
                    "role": role,
                    "processed_this_run": processed,
                    "state": row["state"],
                    "attempts": row["attempts"],
                    "error_class": row["last_error"],
                    "worker_call_seconds_monotonic": time.monotonic() - before,
                }
            )
        passed = all(row["state"] == "accepted" and row["processed_this_run"] for row in result)
        print(
            json.dumps(
                {
                    "status": "PASS" if passed else "BLOCKED",
                    "bot_authentication": "PASS",
                    "recipients": result,
                    "scope": "Telegram API acceptance only; device delivery/viewing and human acknowledgement not measured",
                },
                indent=2,
            )
        )
        return 0 if passed else 2
    except RemoteError as error:
        print(json.dumps({"status": "FAIL", "provider_status": error.status, "secrets_logged": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
