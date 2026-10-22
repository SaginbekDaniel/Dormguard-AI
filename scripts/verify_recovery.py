"""Disposable SQLite crash/restart, relocated evidence restore and outbox replay.

Creates all source and destination data itself. No user database path, production
backup, provider credentials or existing footage can be passed to this verifier.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def child(root):
    from dormguard.store import Store
    from scripts.verify_rtsp import generate_fixture

    store = Store(root)
    director = store.provision_user("director@example.invalid", "director")
    deputy = store.provision_user("deputy@example.invalid", "deputy_director")
    camera = store.create_camera("Generated restore fixture", "Lab", "test", "TEST", director["id"])
    source = generate_fixture(root / "generated.avi")
    incident = store.create_incident(
        camera["id"], 0.8, "CONTROLLED RESTORE FIXTURE", evidence_source=source, clip_start=0, clip_end=2
    )
    with store.db.transaction() as conn:
        conn.execute("INSERT INTO telegram_bindings VALUES(?,?,?)", (director["id"], "101", "test"))
        conn.execute("INSERT INTO telegram_bindings VALUES(?,?,?)", (deputy["id"], "102", "test"))
        # Simulate a crashed claimant after lease commit, before transport.
        conn.execute(
            "UPDATE notification_outbox SET state='sending',lease_token='controlled-crashed-owner',lease_until=0,attempts=1"
        )
    (root / "fixture.json").write_text(json.dumps({"director": director["id"], "incident": incident["id"]}))
    os._exit(23)  # Deliberate abrupt process exit after committed durable data.


def run():
    from fastapi.testclient import TestClient

    from dormguard.app import create_app
    from dormguard.config import Settings
    from dormguard.notifications import TelegramWorker
    from dormguard.playback import browser_derivative, digest_file
    from dormguard.security import hash_password
    from dormguard.store import Store

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="dormguard-recovery-") as directory:
        root = Path(directory)
        original, restored = root / "original", root / "restored"
        original.mkdir(mode=0o700)
        environment = {k: v for k, v in os.environ.items() if k in {"PATH", "TMPDIR", "LANG"}}
        crashed = subprocess.run(
            [sys.executable, __file__, "--child", str(original)],
            env=environment,
            timeout=30,
            capture_output=True,
        )
        assert crashed.returncode == 23, "Controlled crash fixture did not complete"
        restarted = Store(original)
        assert restarted.verify_audit()["valid"]
        before = restarted.incidents()
        assert len(before) == 1
        # Writers are stopped. SQLite backup includes committed WAL data; copy
        # originals at the same quiescent boundary. No caches/sessions exported.
        restored.mkdir(mode=0o700)
        with sqlite3.connect(original / "dormguard.sqlite3") as source:
            with sqlite3.connect(restored / "dormguard.sqlite3") as target:
                source.backup(target)
        shutil.copytree(original / "evidence", restored / "evidence")
        store = Store(restored)
        assert len(store.incidents()) == len(before)
        assert store.verify_audit()["valid"]
        with store.db.read() as conn:
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        fixture = json.loads((original / "fixture.json").read_text())
        incident = store.incident(fixture["incident"])
        evidence = incident["evidence"][0]
        item = store.evidence(evidence["id"], True)
        assert store.verify_evidence(evidence["id"], fixture["director"])["valid"]
        derivative = browser_derivative(store, item)
        assert derivative.is_file() and digest_file(store.evidence_path(item)) == evidence["sha256"]
        password = "controlled-restore-verification-password"
        with store.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), fixture["director"])
            )
        with TestClient(
            create_app(Settings(restored, auth_mode="local", public_url="http://testserver"), store)
        ) as client:
            assert client.get(f"/api/v1/evidence/{evidence['id']}/video").status_code == 401
            assert (
                client.post(
                    "/api/v1/auth/login", json={"email": "director@example.invalid", "password": password}
                ).status_code
                == 200
            )
            response = client.get(f"/api/v1/evidence/{evidence['id']}/video")
            assert response.status_code == 200 and len(response.content) > 64
        calls = []

        def controlled_provider(*args, **kwargs):
            calls.append(kwargs["body"])
            return {"ok": True, "result": {"message_id": len(calls)}}

        worker = TelegramWorker(
            store, Settings(restored, telegram_bot_token="CONTROLLED_RESTORE_TOKEN"), controlled_provider
        )
        assert worker.run_once() and worker.run_once() and not worker.run_once()
        assert len(calls) == 2 and len({call["chat_id"] for call in calls}) == 2
        assert store.operational_summary()["outbox_counts"] == {"accepted": 2}
        return {
            "status": "PASS",
            "runtime_seconds": time.monotonic() - started,
            "crash_exit_code": crashed.returncode,
            "restored_incidents": len(before),
            "evidence_hash_matches": True,
            "relocated_playback": "PASS",
            "anonymous_media_denied": True,
            "foreign_keys": "PASS",
            "audit_chain": "PASS",
            "recovered_recipient_claims": len(calls),
            "provider": "CONTROLLED_TEST_DOUBLE; no external API calls",
            "scope": "generated fixture, quiescent SQLite snapshot + original evidence copy; production/PostgreSQL/object storage restore NOT_RUN",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-disposable", action="store_true")
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.child:
        # Child paths are a private implementation detail of this self-contained
        # verifier. An empty directory and bounded temporary prefix are required.
        if (
            not args.child.name == "original"
            or not args.child.parent.name.startswith("dormguard-recovery-")
            or any(args.child.iterdir())
        ):
            parser.error("refusing non-disposable child destination")
        child(args.child)
    if not args.allow_disposable:
        print(json.dumps({"status": "BLOCKED", "reason": "explicit_disposable_verification_required"}))
        return 2
    result = run()
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
