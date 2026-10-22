"""Read-only source-transfer inventory; does not validate application behavior."""

import argparse
import json
from pathlib import Path


# Historical expected source surface, not a license to synthesize missing files.
REQUIRED_FILES = (
    "README.md",
    "requirements.txt",
    "requirements-dev.txt",
    ".env.example",
    "Dockerfile",
    "compose.yaml",
    "dormguard/app.py",
    "dormguard/store.py",
    "dormguard/database.py",
    "dormguard/security.py",
    "dormguard/supabase_auth.py",
    "dormguard/model.py",
    "dormguard/worker.py",
    "dormguard/notifications.py",
    "dormguard/transport.py",
    "dormguard/playback.py",
    "dormguard/maintenance.py",
    "dormguard/cli.py",
    "dormguard/index.html",
    "dormguard/app.js",
    "dormguard/style.css",
    "migrations/001_schema.sql",
    "migrations/002_rls.sql",
    "tests/test_security_and_delivery.py",
    "scripts/local.sh",
    "scripts/verify_postgres.py",
    "scripts/verify_real_model.py",
    "scripts/verify_rtsp.py",
)


def inspect_source(root: Path) -> dict:
    """Check fixed paths using metadata only, without reading private contents."""
    root = root.resolve()
    problems = []
    for relative in REQUIRED_FILES:
        candidate = root / relative
        try:
            if not candidate.resolve().is_relative_to(root):
                reason = "outside_root"
            elif not candidate.is_file():
                reason = "missing_or_not_file"
            elif candidate.stat().st_size == 0:
                reason = "empty"
            else:
                continue
        except (OSError, RuntimeError):
            reason = "unreadable_path"
        problems.append({"path": relative, "reason": reason})
    return {
        "check": "source_transfer_inventory",
        "status": "BLOCKED" if problems else "PASS",
        "required_files": len(REQUIRED_FILES),
        "problems": problems,
        "scope": "File presence only. No application imports, tests or integrations executed.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    report = inspect_source(args.root)
    print(json.dumps(report, indent=2))
    return 2 if report["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
