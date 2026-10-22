"""Non-destructive retention and evidence reconciliation report.

No deletion command exists. Applying institutional retention, legal holds and
backup expiry requires a separately reviewed policy and explicit authorization.
"""

import json
import time

from .config import load_settings
from .playback import digest_file
from .store import DomainError, Store


def report(store, retention_days):
    cutoff = time.time() - retention_days * 86400
    with store.db.read() as conn:
        items = [
            dict(row)
            for row in conn.execute(
                "SELECT e.*,i.status FROM evidence e JOIN incidents i ON i.id=e.incident_id"
            ).fetchall()
        ]
    known = {item["path"] for item in items}
    missing, mismatched, unreadable = [], [], []
    for item in items:
        try:
            path = store.evidence_path(item)
            if not path.is_file():
                missing.append(item["id"])
            elif path.stat().st_size != item["size_bytes"] or digest_file(path) != item["sha256"]:
                mismatched.append(item["id"])
        except (OSError, DomainError):
            unreadable.append(item["id"])
    candidates = [
        item["id"]
        for item in items
        if item["status"] in {"CLOSED", "FALSE_POSITIVE"} and item["clip_end"] < cutoff
    ]
    orphan_count = sum(
        str(path.relative_to(store.data_dir)) not in known
        for path in store.evidence_dir.iterdir()
        if path.is_file()
    )
    return {
        "mode": "dry_run",
        "retention_days": retention_days,
        "candidate_evidence_ids": candidates,
        "missing_evidence_ids": missing,
        "mismatched_evidence_ids": mismatched,
        "unreadable_or_unsafe_evidence_ids": unreadable,
        "unclassified_file_count": orphan_count,
        "deleted": 0,
        "eligible_for_automatic_deletion": [],
        "policy_status": "BLOCKED: institution retention, holds, deletion authority and backup expiry not approved",
        "limitation": "No automatic deletion or legal-hold policy is enabled",
    }


def main():
    settings = load_settings()
    print(
        json.dumps(report(Store(settings.data_dir, settings.database_url), settings.retention_days), indent=2)
    )


if __name__ == "__main__":
    main()
