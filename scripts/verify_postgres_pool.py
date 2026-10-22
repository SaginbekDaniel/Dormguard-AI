#!/usr/bin/env python3
"""Pool regressions on a NEW EMPTY LOOPBACK dormguard_test_pool_* database.

No .env, hosted target, deletion, real footage or provider. Leaves disposable SQL
for inspection. Tests real reuse, rollback, role/settings reset, concurrency and
pool shutdown; controlled values are explicitly labeled.
"""

import argparse
import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def verify(url, approved):
    import psycopg

    from dormguard.database import Database, DatabaseUnavailable

    parsed = urlsplit(url)
    if (
        not approved
        or parsed.scheme not in ("postgres", "postgresql")
        or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
        or not unquote(parsed.path.lstrip("/")).startswith("dormguard_test_pool_")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Explicit empty loopback pool-test database required")
    started = time.monotonic()
    with psycopg.connect(url) as connection:
        if connection.execute("SELECT 1 FROM pg_tables WHERE schemaname='public'").fetchone():
            raise ValueError("Refusing nonempty database")
    checks = {}
    with tempfile.TemporaryDirectory(prefix="dormguard-pool-test-") as directory:
        db = Database(Path(directory), url)
        try:
            pids = []
            for _ in range(8):
                with db.read() as conn:
                    pids.append(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
                    assert (
                        conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
                    )
                    assert (
                        conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
                        == "repeatable read"
                    )
            assert len(set(pids)) < len(pids), "No connection reuse observed"
            checks["actual_reuse_and_read_only_snapshots"] = "PASS"
            with db.read() as conn:
                baseline = conn.execute(
                    "SELECT current_user AS actor,current_setting('search_path') AS path"
                ).fetchone()
            with db.transaction() as conn:
                conn.execute("SELECT set_config('request.jwt.claim.sub','CONTROLLED_UNTRUSTED',false)")
                conn.execute("SET search_path=pg_catalog")
                conn.execute("SET ROLE pg_monitor")
            for _ in range(16):
                with db.read() as conn:
                    state = conn.execute(
                        "SELECT current_user AS actor,current_setting('search_path') AS path,current_setting('request.jwt.claim.sub',true) AS claim"
                    ).fetchone()
                    assert (
                        state["actor"] == baseline["actor"]
                        and state["path"] == baseline["path"]
                        and not state["claim"]
                    ), "Session state leaked across checkout"
            with db.transaction() as conn:
                assert conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "off"
            checks["role_settings_and_read_only_do_not_leak"] = "PASS"
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO worker_health VALUES('controlled-rollback','test','CONTROLLED','{}',1)"
                    )
                    raise ValueError("Controlled application failure")
            except ValueError:
                pass
            with db.read() as conn:
                assert (
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM worker_health WHERE name='controlled-rollback'"
                    ).fetchone()["n"]
                    == 0
                )
            checks["failed_write_rolls_back"] = "PASS"
            try:
                with db.read() as conn:
                    conn.execute("SELECT 1/0")
            except psycopg.errors.DivisionByZero:
                pass
            with db.read() as conn:
                assert conn.execute("SELECT 1 AS n").fetchone()["n"] == 1
            checks["failed_read_connection_recovers_without_transaction_leak"] = "PASS"
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO worker_health VALUES('controlled-broken','test','CONTROLLED','{}',1)"
                    )
                    conn.raw.close()
            except psycopg.OperationalError:
                pass
            with db.read() as conn:
                assert (
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM worker_health WHERE name='controlled-broken'"
                    ).fetchone()["n"]
                    == 0
                )
            checks["broken_connection_discarded_without_replaying_write"] = "PASS"

            def concurrent(_):
                with db.read() as conn:
                    return conn.execute("SELECT pg_backend_pid() AS pid,pg_sleep(0.02)").fetchone()["pid"]

            with ThreadPoolExecutor(max_workers=24) as executor:
                concurrent_pids = list(executor.map(concurrent, range(48)))
            assert len(set(concurrent_pids)) <= 8
            assert db._pool.get_stats()["pool_size"] <= 8
            checks["48_concurrent_reads_bounded_to_eight_connections"] = "PASS"
            # Hold actual pool connections outside adapter slots to separately
            # exercise the driver's exhausted-checkout deadline and recovery.
            with ExitStack() as borrowed:
                for _ in range(8):
                    raw = borrowed.enter_context(db._pool.connection())
                    raw.execute("SELECT 1")
                exhausted_at = time.monotonic()
                with patch("dormguard.database.ACQUIRE_TIMEOUT_SECONDS", 0.05):
                    try:
                        with db.read():
                            raise AssertionError("Exhausted pool entered a read")
                    except DatabaseUnavailable as error:
                        assert str(error) == "Database temporarily unavailable; try again shortly"
                assert 0.04 <= time.monotonic() - exhausted_at < 1
            with db.read() as conn:
                assert conn.execute("SELECT 1 AS n").fetchone()["n"] == 1
            checks["actual_pool_exhaustion_is_bounded_sanitized_and_recovers"] = "PASS"
            with db.read() as conn:
                held = conn.raw
                db.close()
                assert conn.execute("SELECT 1 AS n").fetchone()["n"] == 1
            assert held.closed
            checks["shutdown_allows_borrowed_read_to_finish_then_closes"] = "PASS"
            try:
                with db.read():
                    pass
                raise AssertionError("Closed pool accepted a new checkout")
            except DatabaseUnavailable:
                pass
            checks["explicit_shutdown_rejects_new_work"] = "PASS"
        finally:
            db.close()
    return {
        "status": "PASS",
        "checks": checks,
        "runtime_seconds": round(time.monotonic() - started, 3),
        "unique_serial_connection_pids": len(set(pids)),
        "unique_concurrent_connection_pids": len(set(concurrent_pids)),
        "scope": "Actual disposable loopback PostgreSQL and official Psycopg pool; no hosted Auth/provider/model/production claim.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--allow-disposable", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.database_url, args.allow_disposable)
    except Exception as error:
        result = {
            "status": "FAIL",
            "error_type": type(error).__name__,
            "detail": "Target retained; connection/exception values suppressed.",
        }
    rendered = json.dumps(result, indent=2) + "\n"
    args.output.write_text(rendered)
    print(rendered)
    raise SystemExit(0 if result["status"] == "PASS" else 1)
