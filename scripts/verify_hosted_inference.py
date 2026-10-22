#!/usr/bin/env python3
"""Real pinned model → hosted PostgreSQL → real hosted Auth → protected review.

Uses only the previously reviewed hash-pinned public-domain boxing film. Boxing
is a consensual-sport negative: an alert is a false alert, not aggression accuracy.
Retains all hosted rows and private evidence. No Telegram send or production deploy.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]


def verify(environment, authorized, review_existing=None):
    from dormguard.config import Settings
    from dormguard.model import load_model
    from dormguard.playback import digest_file
    from dormguard.store import Store
    from dormguard.worker import Worker
    from scripts.verify_hosted import PROJECT_REFERENCE, configuration, pinned_database_url

    if not authorized:
        raise ValueError("Explicit public evaluation and model rights authorization required")
    url, key, identities = configuration(environment, PROJECT_REFERENCE, True)
    root = Path(environment["DORMGUARD_HOSTED_E2E_DATA"]).resolve()
    if (
        root.stat().st_mode & 0o077
        or json.loads((root / "hosted-scope.json").read_text())["project_ref"] != PROJECT_REFERENCE
    ):
        raise ValueError("Pinned retained hosted workspace required")
    database_url = pinned_database_url(
        environment["DORMGUARD_DATABASE_URL"], PROJECT_REFERENCE, environment["DORMGUARD_HOSTED_CA_CERT"]
    )
    settings = Settings(
        root,
        database_url=database_url,
        auth_mode="supabase",
        public_url="http://127.0.0.1:8878",
        supabase_url=url,
        supabase_anon_key=key,
        model_manifest=ROOT / "docs/engineering/MODEL_MANIFEST_EXAMPLE.json",
        model_rights_confirmed=True,
    )
    store = Store(root, database_url)
    director = identities[0]
    assigned = store.user(director["id"])
    if not assigned or assigned["role"] != "director" or not assigned["enabled"]:
        raise ValueError("Approved controlled director assignment required")
    source = next(
        sample
        for sample in json.loads((ROOT / "docs/engineering/PUBLIC_EVALUATION_SOURCES.json").read_text())[
            "samples"
        ]
        if sample["id"] == "positive-boxing"
    )
    original = ROOT / "test-data/public-evaluation" / source["file"]
    if digest_file(original) != source["sha256"]:
        raise ValueError("Reviewed public input hash differs")
    if review_existing:
        started = time.monotonic()
        with store.db.read() as conn:
            event = conn.execute(
                "SELECT i.id,i.status FROM incidents i JOIN cameras c ON c.id=i.camera_id WHERE i.id=? AND i.model=? AND c.source_ref=?",
                (review_existing, "jaranohaal-vit-frame-screening", "DORMGUARD_CAMERA_HOSTED_BOXING"),
            ).fetchone()
        if not event or event["status"] != "DETECTED":
            raise ValueError("An unreviewed actual-worker test event is required")
        try:
            result = review_incident(settings, store, director, event["id"])
            result.update(
                project_ref=PROJECT_REFERENCE,
                inference_executed_in_this_invocation=False,
                provenance="Review of the exact retained real-model event from the interrupted initial full-frame hosted worker run. No controlled incident substituted.",
                source_original_unchanged=digest_file(original) == source["sha256"],
                runtime_seconds=round(time.monotonic() - started, 3),
            )
            if not result["source_original_unchanged"]:
                result["status"] = "FAIL"
            return result
        finally:
            store.db.close()
    directory = root / "test_sources"
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / ("public-boxing-" + secrets.token_hex(6) + ".mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-ss",
            str(source["evaluation_start_seconds"]),
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(original),
            "-t",
            str(source["evaluation_end_seconds"] - source["evaluation_start_seconds"]),
            "-vf",
            "scale=320:240,fps=2",
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
    reference = "DORMGUARD_CAMERA_HOSTED_BOXING"
    with store.db.read() as conn:
        camera = conn.execute("SELECT * FROM cameras WHERE source_ref=?", (reference,)).fetchone()
        previous = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM incidents WHERE model='jaranohaal-vit-frame-screening'"
            ).fetchall()
        }
    if not camera:
        camera = store.create_camera(
            "Public-domain consensual boxing — sport negative",
            "1894 public film; real inference evaluation",
            "test",
            reference,
            director["id"],
        )
    started = time.monotonic()
    model = load_model(settings)
    worker = Worker(settings, store, model=model)
    exceeded = threading.Event()

    def stop_at_budget():
        exceeded.set()
        worker.stop.set()

    deadline = threading.Timer(180, stop_at_budget)
    prior = os.environ.get(reference)
    os.environ[reference] = str(target)
    try:
        deadline.start()
        worker.run(once=True, camera_id=camera["id"], max_seconds=60)
    finally:
        deadline.cancel()
        if prior is None:
            os.environ.pop(reference, None)
        else:
            os.environ[reference] = prior
    incidents = [
        item
        for item in store.incidents()
        if item["id"] not in previous and item["camera_id"] == camera["id"] and item["model"] == model.name
    ]
    result = {
        "status": "PASS" if worker.inferred and not worker.errors and not exceeded.is_set() else "FAIL",
        "project_ref": PROJECT_REFERENCE,
        "model": model.name,
        "real_checkpoint": True,
        "inference_windows": worker.inferred,
        "worker_errors": worker.errors,
        "dropped_windows": worker.dropped,
        "worker_wall_budget_seconds": 180,
        "worker_budget_exceeded": exceeded.is_set(),
        "test_source_fps": 2,
        "test_source_seconds": source["evaluation_end_seconds"] - source["evaluation_start_seconds"],
        "runtime_seconds": round(time.monotonic() - started, 3),
        "consensual_sport_false_alerts": len(incidents),
        "source_original_unchanged": digest_file(original) == source["sha256"],
        "source_sha256": source["sha256"],
        "derived_source_sha256": digest_file(target),
        "scope": "Real pinned ViT and existing worker, reviewed sport-negative public film, hosted SQL. Hosted Auth/review is exercised only when workflow is PASS; no accuracy/occlusion/Telegram/production claim.",
    }
    if not incidents:
        result["workflow"] = "NOT_RUN: actual model produced no new event; no fixture substituted"
        return result
    inference_status = result["status"]
    result.update(review_incident(settings, store, director, incidents[0]["id"]))
    if inference_status != "PASS":
        result["status"] = "FAIL"
    if not result["source_original_unchanged"]:
        result["status"] = "FAIL"
    result["runtime_seconds"] = round(time.monotonic() - started, 3)
    return result


def review_incident(settings, store, director, incident_id):
    from fastapi.testclient import TestClient

    from dormguard.app import create_app
    from dormguard.supabase_auth import SupabaseAuth

    result = {"status": "PASS"}
    provider = SupabaseAuth(settings)
    with TestClient(create_app(settings, store, provider)) as client:
        response = client.post(
            "/api/v1/auth/login", json={"email": director["email"], "password": director["password"]}
        )
        if response.status_code != 200:
            raise ValueError("Real hosted login failed")
        headers = {"X-CSRF-Token": response.json()["csrf_token"]}
        try:
            incident = client.get("/api/v1/incidents/" + incident_id).json()
            hashes = []
            for evidence in incident["evidence"]:
                verified = client.post("/api/v1/evidence/" + evidence["id"] + "/verify", headers=headers)
                playback = client.get("/api/v1/evidence/" + evidence["id"] + "/video")
                if (
                    verified.status_code != 200
                    or not verified.json()["valid"]
                    or playback.status_code != 200
                    or len(playback.content) < 64
                ):
                    raise ValueError("Protected evidence verification failed")
                hashes.append(evidence["sha256"])
            reason = "Controlled verification: public-domain consensual boxing is a sport negative. Model alert is a false positive; no dormitory or disciplinary conclusion."
            for action in ("notes", "acknowledge", "false-positive"):
                changed = client.post(
                    "/api/v1/incidents/" + incident["id"] + "/" + action,
                    headers=headers,
                    json={"reason": reason},
                )
                if changed.status_code != 200:
                    raise ValueError("Audited test-review transition failed")
            final = client.get("/api/v1/incidents/" + incident["id"]).json()
            with store.db.read() as conn:
                recipients = conn.execute(
                    "SELECT COUNT(*) AS n FROM notification_outbox WHERE incident_id=?", (incident["id"],)
                ).fetchone()["n"]
            result.update(
                workflow="PASS",
                incident_id=incident["id"],
                final_status=final["status"],
                evidence_objects=len(hashes),
                evidence_sha256=hashes,
                persisted_outbox_recipients=recipients,
                audit_integrity=store.verify_audit()["valid"],
            )
            if (
                final["status"] != "FALSE_POSITIVE"
                or not hashes
                or recipients != 2
                or not result["audit_integrity"]
            ):
                result["status"] = "FAIL"
        finally:
            logout = client.post("/api/v1/auth/logout", headers=headers)
            result["logout"] = (
                "PASS"
                if logout.status_code == 200 and client.get("/api/v1/incidents").status_code == 401
                else "FAIL"
            )
            if result["logout"] != "PASS":
                result["status"] = "FAIL"
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-and-model-rights-authorized", action="store_true")
    parser.add_argument(
        "--review-existing-real-event",
        help="Exact retained actual-model test event UUID; skips inference and labels prior-run provenance",
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = verify(
            os.environ, arguments.evaluation_and_model_rights_authorized, arguments.review_existing_real_event
        )
    except Exception as error:
        result = {
            "status": "FAIL",
            "error_type": type(error).__name__,
            "detail": "Provider/SQL/configuration exception values suppressed; retain all hosted and private evidence.",
        }
    rendered = json.dumps(result, indent=2) + "\n"
    arguments.output.write_text(rendered)
    print(rendered)
    raise SystemExit(0 if result["status"] == "PASS" else 1)
