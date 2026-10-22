#!/usr/bin/env python3
"""Create approved, labeled non-production Auth users without sending email.

Pinned to the single authorized project. Uses Auth admin/users with email_confirm
true, never signup/invite/recovery. Does not assign any hosted SQL role. Retains
generated credentials privately for test continuation; never resets existing users.
"""

import argparse
import base64
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_hosted import (  # noqa: E402
    IDENTITIES,
    PROJECT_REFERENCE,
    Blocked,
    project_configuration,
)


def create(environment, reference, approved, output):
    from dormguard.config import Settings
    from dormguard.supabase_auth import SupabaseAuth

    url, key = project_configuration(environment, reference, approved)
    admin_key = environment.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not admin_key:
        raise Blocked("Configure the authorized project's Auth admin credential privately")
    if not admin_key.startswith("sb_secret_"):
        try:
            payload = admin_key.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if claims.get("role") != "service_role" or claims.get("ref") != PROJECT_REFERENCE:
                raise ValueError
        except (ValueError, IndexError, TypeError, AttributeError):
            raise Blocked("Admin credential is not a service key for the authorized project") from None
    output = Path(output).absolute()
    parent = output.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_mode & 0o077:
        raise Blocked("Output parent must already be a private directory (mode 0700)")
    # No overwrite/symlink following. Keep each successful credential before
    # attempting another user; failures must not strand unrecorded test identities.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    values = {"DORMGUARD_HOSTED_TEST_IDENTITIES_APPROVED": "1"}
    created = 0
    try:
        provider = SupabaseAuth(Settings(parent, supabase_url=url, supabase_anon_key=key))
        run = secrets.token_hex(6)
        with os.fdopen(fd, "w") as target:

            def persist():
                target.seek(0)
                json.dump(values, target)
                target.truncate()
                target.flush()
                os.fsync(target.fileno())

            for label in IDENTITIES:
                email = "dormguard-verify-" + label.lower().replace("_", "-") + "-" + run + "@example.invalid"
                password = secrets.token_urlsafe(36)
                prefix = "DORMGUARD_HOSTED_TEST_" + label
                values.update({prefix + "_EMAIL": email, prefix + "_PASSWORD": password})
                persist()  # Preserve intent if acceptance/response is interrupted.
                value = provider._call(
                    "POST",
                    "admin/users",
                    body={
                        "email": email,
                        "password": password,
                        "email_confirm": True,
                        "user_metadata": {
                            "purpose": "Controlled DormGuard verification",
                            "role": "super_admin",
                            "verification_run": run,
                        },
                    },
                    admin_key=admin_key,
                )
                if not isinstance(value.get("id"), str) or value.get("email", "").lower() != email:
                    raise Blocked(
                        "Admin response did not establish the controlled identity; inspect project privately"
                    )
                values[prefix + "_ID"] = value["id"]
                created += 1
                persist()
    except Exception:
        # Do not delete partially created identities or their private recovery file.
        raise
    return {
        "status": "PASS",
        "project_ref": reference,
        "created_test_users": created,
        "emails_sent_by_this_script": 0,
        "hosted_sql_roles_assigned": 0,
        "scope": "Controlled Auth identities created; login/RLS/application verification still required. Editable metadata intentionally claims super_admin to test that SQL grants remain authoritative.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--allow-controlled-test-users", action="store_true")
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    try:
        result = create(
            os.environ, arguments.project_ref, arguments.allow_controlled_test_users, arguments.output
        )
    except Blocked as error:
        result = {"status": "BLOCKED", "detail": str(error)}
    except Exception as error:
        result = {
            "status": "FAIL",
            "error_type": type(error).__name__,
            "detail": "Creation did not complete. Retain the private output file and inspect the authorized project before retrying. No credentials or exception values recorded.",
        }
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 2 if result["status"] == "BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
