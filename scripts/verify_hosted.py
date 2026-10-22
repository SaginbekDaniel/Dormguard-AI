#!/usr/bin/env python3
"""Opt-in hosted Auth/RLS verification. Never migrates or provisions a hosted DB.

Secrets come only from explicit environment variables; no .env loading, raw
provider responses or exception text in reports. Application writes use a NEW
temporary SQLite workspace. Hosted operations are Auth login/validation/logout,
Data API GETs, and optionally a read-only SQL catalog transaction.
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dormguard.transport import NoRedirect, RemoteError  # noqa: E402

PROJECT_NAME = "dormguard AI"
PROJECT_REFERENCE = "sgpeplsengohysoiomyl"
IDENTITIES = ("DIRECTOR", "DEPUTY", "SUPER_ADMIN", "TECHNICAL_ADMIN", "UNASSIGNED")
ROLES = ("director", "deputy_director", "super_admin", "technical_admin", None)
TABLES = (
    "schema_migrations",
    "users",
    "sessions",
    "stream_tickets",
    "cameras",
    "camera_health",
    "worker_health",
    "incidents",
    "incident_keys",
    "events",
    "evidence",
    "telegram_bindings",
    "telegram_links",
    "notification_outbox",
    "notification_attempts",
    "audit_logs",
)


class Blocked(Exception):
    """Messages are fixed diagnostic labels; never contain configuration values."""


def project_configuration(environment, reference, consent):
    if not consent or environment.get("DORMGUARD_HOSTED_TEST_IDENTITIES_APPROVED") != "1":
        raise Blocked("Explicit non-production and controlled-test-identity authorization required")
    if environment.get("DORMGUARD_HOSTED_PROJECT_NAME") != PROJECT_NAME:
        raise Blocked("Exact authorized project name has not been confirmed")
    if not re.fullmatch(r"[a-z0-9]{20}", reference or "") or reference != PROJECT_REFERENCE:
        raise Blocked("A verified twenty-character project reference is required")
    if environment.get("DORMGUARD_HOSTED_PROJECT_REF") != reference:
        raise Blocked("Command and private environment project pins differ")
    url = environment.get("SUPABASE_URL", "").rstrip("/")
    if url != f"https://{reference}.supabase.co":
        raise Blocked("Supabase URL does not match the exact authorized reference")
    key = environment.get("SUPABASE_ANON_KEY", "")
    if not key or key.startswith("sb_secret_"):
        raise Blocked("A publishable or legacy anon API key is required; service keys are forbidden")
    if not key.startswith("sb_publishable_"):
        try:
            payload = key.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if claims.get("role") != "anon" or claims.get("ref") != reference:
                raise ValueError
        except (IndexError, ValueError, TypeError, AttributeError):
            raise Blocked("Legacy API key is not an anon key for the authorized project") from None
    return url, key


def configuration(environment, reference, consent):
    url, key = project_configuration(environment, reference, consent)
    identities = []
    for label, role in zip(IDENTITIES, ROLES):
        prefix = "DORMGUARD_HOSTED_TEST_" + label
        identity = {
            field: environment.get(prefix + "_" + field.upper(), "") for field in ("id", "email", "password")
        }
        try:
            uuid.UUID(identity["id"])
        except ValueError:
            raise Blocked("Every approved test identity requires its existing provider UUID") from None
        if not identity["email"].lower().startswith("dormguard-verify-") or not identity["password"]:
            raise Blocked(
                "Only explicitly labeled dormguard-verify- test identities with credentials are accepted"
            )
        identities.append({**identity, "label": label, "role": role})
    if len({identity["id"] for identity in identities}) != len(identities) or len(
        {identity["email"].lower() for identity in identities}
    ) != len(identities):
        raise Blocked("Test identities must have distinct provider UUIDs and emails")
    return url, key, identities


def data_api_denial(url, key, table, token=None):
    headers = {"apikey": key}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url + "/rest/v1/" + table + "?select=*&limit=1", headers=headers)
    try:
        with build_opener(NoRedirect()).open(request, timeout=10):
            # Even [] is a failure of this application's no-browser-grants contract.
            # Do not read or report records if configuration unexpectedly exposes them.
            return "FAIL"
    except HTTPError as error:
        try:
            result = json.loads(error.read(65536))
        except (ValueError, OSError):
            return "BLOCKED"
        denied_statuses = {403} if token else {401, 403}
        if error.code in denied_statuses and isinstance(result, dict) and result.get("code") == "42501":
            return "PASS"
        return "BLOCKED"  # Missing table, invalid key, outage and 401 are not RLS passes.
    except (URLError, TimeoutError, OSError):
        return "BLOCKED"


def pinned_database_url(database_url, reference, certificate):
    """Pin direct or the user-provided session pooler; always verify CA/hostname."""
    parsed = urlsplit(database_url)
    if reference != PROJECT_REFERENCE or parsed.scheme not in {"postgres", "postgresql"}:
        raise Blocked("Database reference is not the authorized project")
    direct = parsed.hostname == f"db.{reference}.supabase.co" and unquote(parsed.username or "") == "postgres"
    pooler = (
        parsed.hostname == "aws-0-ap-south-1.pooler.supabase.com"
        and unquote(parsed.username or "") == "postgres." + reference
    )
    if (
        not (direct or pooler)
        or parsed.port not in (None, 5432)
        or parsed.path != "/postgres"
        or not parsed.password
    ):
        raise Blocked("Database connection does not identify the authorized project")
    if parsed.query or parsed.fragment:
        raise Blocked("Database URL cannot override target or TLS options")
    root = Path(certificate or "").absolute()
    if not certificate or root.is_symlink() or not root.is_file():
        raise Blocked("Configure the downloaded official Supabase CA certificate")
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode({"sslmode": "verify-full", "sslrootcert": str(root)}),
            "",
        )
    )


def catalog(database_url, reference, certificate=None):
    """Optional: inspect exact target in a READ ONLY transaction; no app initializer."""
    import psycopg
    from psycopg.rows import dict_row

    verified_url = pinned_database_url(database_url, reference, certificate)
    with psycopg.connect(
        verified_url,
        row_factory=dict_row,
        connect_timeout=10,
        sslmode="verify-full",
        options="-c default_transaction_read_only=on",
    ) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        if conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] != "on":
            raise Blocked("Read-only transaction was not established")
        conn.execute("SET LOCAL statement_timeout='10s'")
        rows = conn.execute(
            "SELECT c.relname,c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind='r' AND c.relname=ANY(%s)",
            (list(TABLES),),
        ).fetchall()
        if {row["relname"] for row in rows} != set(TABLES):
            return "BLOCKED"
        if not all(row["relrowsecurity"] for row in rows):
            return "FAIL"
        if conn.execute(
            "SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename=ANY(%s)", (list(TABLES),)
        ).fetchone():
            return "FAIL"
        for role in ("anon", "authenticated"):
            for table in TABLES:
                if conn.execute(
                    "SELECT has_table_privilege(%s,%s,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS granted",
                    (role, "public." + table),
                ).fetchone()["granted"]:
                    return "FAIL"
            for function in ("public.dg_role()", "public.dg_append_only()"):
                if conn.execute(
                    "SELECT has_function_privilege(%s,%s,'EXECUTE') AS granted", (role, function)
                ).fetchone()["granted"]:
                    return "FAIL"
        conn.rollback()
        return "PASS"


def application_checks(settings, identities, provider):
    from fastapi.testclient import TestClient

    from dormguard.app import COOKIE, create_app
    from dormguard.store import Store

    store = Store(settings.data_dir)  # Always NEW local SQLite, never hosted DB.
    for identity in identities:
        if identity["role"]:
            store.provision_user(identity["email"], identity["role"], user_id=identity["id"])
    app = create_app(settings, store, provider)
    clients = []
    try:
        for identity in identities:
            client = TestClient(app)
            clients.append(client)
            result = client.post(
                "/api/v1/auth/login", json={"email": identity["email"], "password": identity["password"]}
            )
            expected = 200 if identity["role"] in {"director", "deputy_director", "super_admin"} else 401
            if result.status_code != expected:
                return "FAIL"
            if expected == 401:
                if client.cookies.get(COOKIE) or client.get("/api/v1/incidents").status_code != 401:
                    return "FAIL"
                continue
            headers = {"X-CSRF-Token": result.json()["csrf_token"]}
            for path in (
                "auth/me",
                "overview",
                "cameras",
                "incidents",
                "evidence",
                "audit",
                "system",
                "integrations",
            ):
                if client.get("/api/v1/" + path).status_code != 200:
                    return "FAIL"
            if client.get("/api/v1/users").status_code != (200 if identity["role"] == "super_admin" else 403):
                return "FAIL"
            if (
                client.patch("/api/v1/auth/profile", json={"display_name": "Controlled verifier"}).status_code
                != 403
            ):
                return "FAIL"
            if (
                client.patch(
                    "/api/v1/auth/profile",
                    headers=headers,
                    json={"display_name": "Controlled verifier", "role": "super_admin"},
                ).status_code
                != 422
            ):
                return "FAIL"
            cookie = client.cookies.get(COOKIE)
            if client.post("/api/v1/auth/logout", headers=headers).status_code != 200:
                return "FAIL"
            client.cookies.set(COOKIE, cookie)
            if client.get("/api/v1/incidents").status_code != 401:
                return "FAIL"
        return "PASS" if store.verify_audit()["valid"] else "FAIL"
    finally:
        for client in clients:
            client.close()


def verify(environment, reference, consent):
    from dormguard.config import Settings
    from dormguard.supabase_auth import SupabaseAuth

    started = time.monotonic()
    url, key, identities = configuration(environment, reference, consent)
    results = {
        "status": "BLOCKED",
        "project_name": PROJECT_NAME,
        "project_ref": reference,
        "checks": {},
        "full_hosted_mvp_acceptance": "NOT_RUN",
        "scope": "Real hosted Auth/Data API; disposable local SQLite application; optional read-only hosted SQL. No deployment, schema/account/role changes, messages or real footage. Browser/media/expiry/disable/provider-outage E2E NOT_RUN by this script.",
    }

    class RecordedProvider(SupabaseAuth):
        """Real adapter with private token tracking solely for scoped test cleanup."""

        tokens = []

        def login(self, email, password):
            value = super().login(email, password)
            self.tokens.append(value["access_token"])
            return value

    with tempfile.TemporaryDirectory(prefix="dormguard-hosted-verify-") as directory:
        settings = Settings(
            Path(directory),
            auth_mode="supabase",
            supabase_url=url,
            supabase_anon_key=key,
            public_url="http://testserver",
        )
        provider = RecordedProvider(settings)
        try:
            tokens = {}
            for identity in identities:
                value = provider.login(identity["email"], identity["password"])
                user = provider.validate(value["access_token"])
                if (
                    user.get("id") != identity["id"]
                    or user.get("email", "").lower() != identity["email"].lower()
                ):
                    raise Blocked("Provider identity does not match the approved test binding")
                tokens[identity["label"]] = value["access_token"]
            results["checks"]["hosted_identity_validation"] = "PASS"
            results["checks"]["application_authorization_local_sql"] = application_checks(
                settings, identities, provider
            )
            probes = {
                label: {table: data_api_denial(url, key, table, token) for table in TABLES}
                for label, token in {"ANONYMOUS": None, **tokens}.items()
            }
            results["checks"]["hosted_data_api_denial"] = probes
            database_url = environment.get("DORMGUARD_HOSTED_READONLY_DATABASE_URL") or environment.get(
                "DORMGUARD_DATABASE_URL"
            )
            results["checks"]["hosted_catalog_grants_rls"] = (
                catalog(database_url, reference, environment.get("DORMGUARD_HOSTED_CA_CERT"))
                if database_url
                else "BLOCKED"
            )
            outcomes = [
                results["checks"]["application_authorization_local_sql"],
                results["checks"]["hosted_catalog_grants_rls"],
                *(state for group in probes.values() for state in group.values()),
            ]
            results["status"] = (
                "FAIL" if "FAIL" in outcomes else "BLOCKED" if "BLOCKED" in outcomes else "PASS"
            )
        finally:
            failures = 0
            for token in set(provider.tokens):
                try:
                    provider.logout(token)
                except RemoteError as error:
                    if error.status not in (401, 403, 404):
                        failures += 1
            results["test_session_cleanup"] = "FAIL" if failures else "PASS"
            if failures:
                results["status"] = "FAIL"
    results["runtime_seconds"] = round(time.monotonic() - started, 3)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--allow-nonproduction-test", action="store_true")
    arguments = parser.parse_args()
    try:
        result = verify(os.environ, arguments.project_ref, arguments.allow_nonproduction_test)
    except Blocked as error:
        result = {"status": "BLOCKED", "detail": str(error)}
    except Exception as error:
        result = {
            "status": "FAIL",
            "error_type": type(error).__name__,
            "detail": "Verification did not complete; inspect configuration privately. No exception values or provider payloads recorded.",
        }
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 2 if result["status"] == "BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
