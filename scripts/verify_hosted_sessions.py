#!/usr/bin/env python3
"""Actual provider/session probes. Credentials only from the pinned test environment.

Tests provider logout/refresh rejection, tampered JWT rejection, and application
expiry on NEW local SQLite. Does not alter global project Auth settings or forge
an expired signed JWT. Provider access-JWT behavior is observed, not assumed.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def verify(environment):
    from fastapi.testclient import TestClient

    from dormguard.app import create_app
    from dormguard.config import Settings
    from dormguard.store import Store
    from dormguard.supabase_auth import SupabaseAuth
    from dormguard.transport import RemoteError
    from scripts.verify_hosted import PROJECT_REFERENCE, configuration

    started = time.monotonic()
    url, key, identities = configuration(environment, PROJECT_REFERENCE, True)
    user = identities[0]
    checks = {}
    tokens = []
    with tempfile.TemporaryDirectory(prefix="dormguard-hosted-session-") as directory:
        settings = Settings(
            Path(directory), supabase_url=url, supabase_anon_key=key, public_url="http://testserver"
        )
        provider = SupabaseAuth(settings)
        try:
            login = provider.login(user["email"], user["password"])
            token = login["access_token"]
            tokens.append(token)
            if provider.validate(token).get("id") != user["id"]:
                raise ValueError("Provider binding mismatch")
            provider.logout(token)
            try:
                refreshed = provider._call(
                    "POST", "token?grant_type=refresh_token", body={"refresh_token": login["refresh_token"]}
                )
                if refreshed.get("access_token"):
                    tokens.append(refreshed["access_token"])
                checks["provider_logout_revokes_refresh_token"] = "FAIL"
            except RemoteError as error:
                checks["provider_logout_revokes_refresh_token"] = (
                    "PASS" if error.status in (400, 401, 403) else "BLOCKED"
                )
            try:
                provider.validate(token + "tampered")
                checks["tampered_access_token_rejected"] = "FAIL"
            except RemoteError as error:
                checks["tampered_access_token_rejected"] = (
                    "PASS" if error.status in (400, 401, 403) else "BLOCKED"
                )
            try:
                provider.validate(token)
                jwt_behavior = "Access JWT remains accepted by /user after logout; no immediate global JWT invalidation guarantee"
            except RemoteError as error:
                jwt_behavior = (
                    "Access JWT rejected after logout"
                    if error.status in (400, 401, 403)
                    else "BLOCKED: provider unavailable"
                )
            store = Store(settings.data_dir)
            store.provision_user(user["email"], "director", user_id=user["id"])
            with TestClient(create_app(settings, store, provider)) as client:
                response = client.post(
                    "/api/v1/auth/login", json={"email": user["email"], "password": user["password"]}
                )
                if response.status_code != 200:
                    raise ValueError("Hosted login failed")
                with store.db.transaction() as conn:
                    tokens.append(
                        conn.execute(
                            "SELECT provider_token FROM sessions WHERE user_id=?", (user["id"],)
                        ).fetchone()["provider_token"]
                    )
                    conn.execute(
                        "UPDATE sessions SET expires_at=? WHERE user_id=?", (time.time() - 1, user["id"])
                    )
                checks["expired_application_session_denies_identity_and_evidence"] = (
                    "PASS"
                    if all(
                        client.get("/api/v1/" + path).status_code == 401
                        for path in ("auth/me", "incidents", "evidence")
                    )
                    else "FAIL"
                )
                checks["local_audit_chain"] = "PASS" if store.verify_audit()["valid"] else "FAIL"
        finally:
            cleanup = "PASS"
            for token in set(tokens):
                try:
                    provider.logout(token)
                except RemoteError as error:
                    if error.status not in (401, 403, 404):
                        cleanup = "FAIL"
    checks["test_session_cleanup"] = cleanup
    return {
        "status": "FAIL"
        if "FAIL" in checks.values()
        else "BLOCKED"
        if "BLOCKED" in checks.values()
        else "PASS",
        "project_ref": PROJECT_REFERENCE,
        "checks": checks,
        "provider_access_jwt_after_logout": jwt_behavior,
        "runtime_seconds": round(time.monotonic() - started, 3),
        "scope": "Real Supabase Auth logout/refresh/tampered-token checks plus application expiry in disposable local SQLite. Provider JWT natural expiry, hosted application SQL and outage E2E are NOT_RUN.",
    }


if __name__ == "__main__":
    try:
        result = verify(os.environ)
    except Exception as error:
        result = {
            "status": "FAIL",
            "error_type": type(error).__name__,
            "detail": "Provider response and configuration values suppressed.",
        }
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
