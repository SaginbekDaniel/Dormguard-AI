#!/usr/bin/env python3
"""Run ONLY against an empty, explicitly disposable PostgreSQL database.

Does not read .env, delete tables, send messages, or touch footage. Creates one
temporary NOLOGIN browser test role inside a transaction that always rolls back.
The target name must begin dormguard_test_. This script leaves its test tables
for inspection; dispose of the entire test database using your own DB tooling.
"""

import argparse
import json
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def verify(database_url):
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    from dormguard.store import DomainError, Store

    start = time.monotonic()
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if not name.startswith("dormguard_test_"):
            raise ValueError("Refusing target without dormguard_test_ database name")
        objects = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')"
        ).fetchall()
        if objects:
            raise ValueError("Refusing nonempty database; create a fresh disposable target")
    with tempfile.TemporaryDirectory(prefix="dormguard-postgres-test-") as directory:
        store = Store(Path(directory), database_url)
        director = store.provision_user("director@example.test", "director")["id"]
        deputy = store.provision_user("deputy@example.test", "deputy_director")["id"]
        admin = store.provision_user("admin@example.test", "technical_admin")["id"]
        super_admin = store.provision_user("super@example.test", "super_admin")["id"]
        store.update_profile(super_admin, "Disposable administrator")
        camera = store.create_camera("Disposable test source", "Lab", "test", "not-real.mp4", director)["id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            incidents = list(
                pool.map(
                    lambda n: store.create_incident(
                        camera, 0.9, "controlled-test-model", event_key="test-event-" + str(n)
                    ),
                    range(16),
                )
            )
        assert len({row["id"] for row in incidents}) == 1, "Concurrent incident deduplication failed"
        incident_id = incidents[0]["id"]
        with store.db.read() as conn:
            recipients = conn.execute("SELECT COUNT(*) AS n FROM notification_outbox").fetchone()["n"]
            assert recipients == 2, "Two-recipient transaction failed"
        for actor in (admin, "unassigned"):
            try:
                store.transition(incident_id, "acknowledge", actor)
                raise AssertionError("Unauthorized actor accepted")
            except DomainError as error:
                assert error.status == 403
        with store.db.transaction() as conn:
            conn.execute("UPDATE users SET enabled=0 WHERE id=?", (deputy,))
        try:
            store.transition(incident_id, "acknowledge", deputy)
            raise AssertionError("Disabled actor accepted")
        except DomainError as error:
            assert error.status == 403
        for action in ("acknowledge", "confirm", "resolve", "close"):
            store.transition(incident_id, action, director, "Disposable test review")
        assert store.verify_audit()["valid"], "Audit hash verification failed"
        assert store.overview()["total_incidents"] == 1, "Overview query failed on PostgreSQL"
        page = store.incident_page(1, q="Disposable test", sort="score")
        assert page["total"] == 1 and page["items"][0]["id"] == incident_id
        assert store.incident_page(q="' OR 1=1 --")["total"] == 0
        assert store.incident_page(from_date="2000-01-01", to_date="2000-01-02")["total"] == 0
        assert store.audit_page(q=camera, category="operations")["total"] >= 1
        assert store.audit_page(q="user.provisioned", category="security")["total"] == 4
        assert store.user(super_admin)["display_name"] == "Disposable administrator"
        with store.db.read() as conn:
            tables = conn.execute(
                "SELECT c.relname,c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r'"
            ).fetchall()
            assert len(tables) == 16 and all(row["relrowsecurity"] for row in tables), "RLS inventory failed"
            # No SELECT/INSERT policies exist: even accidental SELECT grants do
            # not expose these records to a nonowner, non-BYPASSRLS client.
            assert (
                conn.execute("SELECT COUNT(*) AS n FROM pg_policies WHERE schemaname='public'").fetchone()[
                    "n"
                ]
                == 0
            )
            for role in ("anon", "authenticated"):
                exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname=?", (role,)).fetchone()
                if exists:
                    for function in ("public.dg_role()", "public.dg_append_only()"):
                        assert not conn.execute(
                            "SELECT has_function_privilege(?,?,'EXECUTE') AS granted", (role, function)
                        ).fetchone()["granted"], "Browser role has function execution privileges"
                    for table in tables:
                        grants = conn.execute(
                            "SELECT has_table_privilege(?,?,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS granted",
                            (role, "public." + table["relname"]),
                        ).fetchone()["granted"]
                        assert not grants, "Browser role has table privileges"
            conn.execute("SELECT set_config('request.jwt.claim.sub',?,true)", (director,))
            assert conn.execute("SELECT public.dg_role() AS role").fetchone()["role"] == "director"
            conn.execute("SELECT set_config('request.jwt.claim.sub',?,true)", (deputy,))
            assert conn.execute("SELECT public.dg_role() AS role").fetchone()["role"] is None
            conn.execute("SELECT set_config('request.jwt.claim.sub',?,true)", ("unassigned",))
            assert conn.execute("SELECT public.dg_role() AS role").fetchone()["role"] is None
        try:
            with store.db.transaction() as conn:
                conn.execute("DELETE FROM audit_logs")
            raise AssertionError("Audit deletion accepted")
        except psycopg.errors.RaiseException:
            pass
        # Exercise RLS as an actual nonowner with no BYPASSRLS. This role and
        # grants are rolled back together, including on failure. Never reuse a
        # live role or change global anon/authenticated permissions here.
        browser_role = "dg_verify_" + uuid.uuid4().hex
        matrix = [
            "anonymous",
            "director",
            "deputy",
            "super_admin",
            "technical_admin",
            "disabled",
            "unassigned",
            "member",
            "student",
        ]
        with store.db.transaction() as conn:
            conn.execute("UPDATE users SET enabled=1 WHERE id=?", (deputy,))
        with psycopg.connect(database_url, row_factory=dict_row) as browser:
            try:
                browser.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT").format(
                        sql.Identifier(browser_role)
                    )
                )
                browser.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(browser_role))
                )
                browser.execute(
                    sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {}").format(
                        sql.Identifier(browser_role)
                    )
                )
                browser.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(browser_role)))
                for label, subject in zip(
                    matrix,
                    ("", director, deputy, super_admin, admin, deputy, "unassigned", "member", "student"),
                ):
                    if label == "disabled":
                        browser.execute("RESET ROLE")
                        browser.execute("UPDATE public.users SET enabled=0 WHERE id=%s", (deputy,))
                        browser.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(browser_role)))
                    browser.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (subject,))
                    for table in tables:
                        count = browser.execute(
                            sql.SQL("SELECT COUNT(*) AS n FROM public.{}").format(
                                sql.Identifier(table["relname"])
                            )
                        ).fetchone()["n"]
                        assert count == 0, "RLS disclosed records to " + label
                    browser.execute("SAVEPOINT denied_write")
                    try:
                        browser.execute(
                            "INSERT INTO public.users(id,email,role,enabled,created_at) VALUES(%s,%s,'director',1,'test')",
                            ("denied-" + label, label + "@denied.test"),
                        )
                        raise AssertionError("Browser role write passed RLS")
                    except psycopg.errors.InsufficientPrivilege:
                        browser.execute("ROLLBACK TO SAVEPOINT denied_write")
                    browser.execute("RELEASE SAVEPOINT denied_write")
            finally:
                browser.rollback()
        return {
            "status": "PASS",
            "runtime_seconds": round(time.monotonic() - start, 3),
            "tables": len(tables),
            "experience_queries": "PASS: literal search, date filters, signal sort and journal categories on PostgreSQL",
            "scope": "Disposable actual PostgreSQL SQL, concurrency, lifecycle, trusted-role helper, RLS grants and nonowner browser-role impersonation, append-only audit. No hosted Supabase Auth, browser JWT/Data API, real footage, inference or provider delivery.",
            "browser_role_matrix": {
                "status": "PASS",
                "identities": matrix,
                "boundary": "All browser identities denied direct database records and role writes; HTTP server mediates authorized access.",
            },
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url", required=True, help="URL of an empty dormguard_test_ database; never a live target"
    )
    parser.add_argument(
        "--allow-disposable",
        action="store_true",
        help="Explicit confirmation this empty database can receive test tables",
    )
    args = parser.parse_args()
    if not args.allow_disposable:
        parser.error("--allow-disposable is required; no connection attempted")
    parsed = urlparse(args.database_url)
    if parsed.scheme not in ("postgres", "postgresql") or not unquote(parsed.path.lstrip("/")).startswith(
        "dormguard_test_"
    ):
        parser.error("Expected PostgreSQL URL naming dormguard_test_*; no connection attempted")
    try:
        print(json.dumps(verify(args.database_url), indent=2))
    except Exception as error:
        # Connection errors can contain private URLs/hostnames. Emit type only.
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error_type": type(error).__name__,
                    "detail": "Verification failed; target retained for private inspection. No credentials printed.",
                }
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
