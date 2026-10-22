"""Actual licensed checkpoint + public film camera → incident → human API review.

Uses disposable storage and reviewed, hash-pinned public-domain test media. Does
not contact Supabase, Telegram, production services or real cameras. Positive
boxing is a sports proxy; this verifier does not establish dormitory accuracy.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]


def run():
    from fastapi.testclient import TestClient

    from dormguard.app import create_app
    from dormguard.config import Settings
    from dormguard.model import load_model
    from dormguard.playback import digest_file
    from dormguard.security import hash_password
    from dormguard.store import Store
    from dormguard.worker import Worker

    sources = json.loads((ROOT / "docs/engineering/PUBLIC_EVALUATION_SOURCES.json").read_text())
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="dormguard-public-journey-") as temporary:
        settings = Settings(
            Path(temporary),
            auth_mode="local",
            public_url="http://testserver",
            model_manifest=ROOT / "docs/engineering/MODEL_MANIFEST_EXAMPLE.json",
            model_rights_confirmed=True,
        )
        store = Store(settings.data_dir)
        password = "controlled-public-journey-password"
        director = store.provision_user("director@example.invalid", "director", hash_password(password))
        store.provision_user("deputy@example.invalid", "deputy_director", hash_password(password))
        directory = settings.data_dir / "test_sources"
        directory.mkdir(mode=0o700)
        refs, cameras, derivatives = {}, {}, []
        for index, sample in enumerate(sources["samples"]):
            original = ROOT / "test-data/public-evaluation" / sample["file"]
            if not original.is_file() or digest_file(original) != sample["sha256"]:
                raise ValueError("reviewed_public_fixture_missing_or_changed")
            target = directory / (sample["id"] + ".mp4")
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    str(sample["evaluation_start_seconds"]),
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(original),
                    "-t",
                    str(sample["evaluation_end_seconds"] - sample["evaluation_start_seconds"]),
                    "-vf",
                    "scale=320:240",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "2",
                    "-n",
                    str(target),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
                timeout=30,
            )
            ref = "DORMGUARD_CAMERA_PUBLIC_" + str(index)
            refs[ref] = str(target)
            camera = store.create_camera(
                sample["title"], "Public-domain evaluation fixture", "test", ref, director["id"]
            )
            cameras[sample["id"]] = camera["id"]
            derivatives.append(
                {
                    "id": sample["id"],
                    "sha256": digest_file(target),
                    "transform": "trim reviewed bounds; scale320x240; H264 CRF18; remove audio; source unchanged",
                }
            )
        model = load_model(settings)
        worker = Worker(settings, store, model=model)
        with patch.dict(os.environ, refs):
            worker.run(once=True)
        incidents = store.incidents()
        positive = [item for item in incidents if item["camera_id"] == cameras["positive-boxing"]]
        false_positives = [item for item in incidents if item["camera_id"] == cameras["negative-greeting"]]
        result = {
            "execution_status": "PASS",
            "model": model.name,
            "real_checkpoint": True,
            "source_scope": sources["scope"],
            "runtime_seconds": round(time.perf_counter() - started, 3),
            "inference_windows": worker.inferred,
            "worker_errors": worker.errors,
            "worker_timings": worker.metrics.snapshot(),
            "dropped_windows": worker.dropped,
            "positive_proxy_incidents": len(positive),
            "negative_false_positive_incidents": len(false_positives),
            "threshold": settings.detection_threshold,
            "derivatives": derivatives,
            "telegram_delivery": "NOT_RUN: no external credentials or sends",
            "hosted_supabase": "NOT_RUN: local password/SQLite evaluation",
            "field_accuracy": "NOT_RUN",
        }
        if not positive:
            result["journey_status"] = (
                "FAIL: real model did not trigger a positive-proxy incident; no synthetic incident substituted"
            )
            return result
        incident = store.incident(positive[0]["id"])
        evidence = incident["evidence"]
        result["pipeline_events"] = [
            event["detail"]
            for event in incident["events"]
            if event["kind"] in {"detected", "pipeline.persisted"}
        ]
        result["telegram_api_latency_seconds"] = None
        result["clock_sync_uncertainty_seconds"] = None
        with TestClient(create_app(settings, store)) as client:
            assert client.get("/api/v1/incidents").status_code == 401
            login = client.post("/api/v1/auth/login", json={"email": director["email"], "password": password})
            assert login.status_code == 200, login.text
            csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
            assert client.get("/api/v1/overview").json()["total_incidents"] == len(incidents)
            hashes = []
            for item in evidence:
                verified = client.post(f"/api/v1/evidence/{item['id']}/verify", headers=csrf)
                assert verified.status_code == 200 and verified.json()["valid"]
                playback = client.get(f"/api/v1/evidence/{item['id']}/video")
                assert playback.status_code == 200 and len(playback.content) > 64
                hashes.append(item["sha256"])
            for action in ("acknowledge", "confirm", "resolve", "close"):
                changed = client.post(
                    f"/api/v1/incidents/{incident['id']}/{action}",
                    json={
                        "reason": "Local evaluation of public-domain boxing proxy; no real dormitory incident"
                    },
                    headers=csrf,
                )
                assert changed.status_code == 200, changed.text
            final = client.get(f"/api/v1/incidents/{incident['id']}").json()
            assert final["status"] == "CLOSED"
            assert client.get("/api/v1/audit").json()["integrity"]["valid"]
            assert client.post("/api/v1/auth/logout", headers=csrf).status_code == 200
            assert client.get("/api/v1/incidents").status_code == 401
        with store.db.read() as conn:
            outbox = conn.execute(
                "SELECT COUNT(*) AS n FROM notification_outbox WHERE incident_id=?", (incident["id"],)
            ).fetchone()["n"]
        result.update(
            journey_status="PASS",
            final_state=final["status"],
            evidence_objects=len(evidence),
            evidence_sha256=hashes,
            actual_incident_score=incident["score"],
            persisted_outbox_recipients=outbox,
            audited_events=len(final["events"]),
            audit_integrity=store.verify_audit()["valid"],
        )
        result["runtime_seconds"] = round(time.perf_counter() - started, 3)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-authorized", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.evaluation_authorized:
        print(json.dumps({"status": "BLOCKED", "reason": "public_evaluation_authorization_required"}))
        return 2
    result = run()
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered)
    return 0 if result["journey_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
