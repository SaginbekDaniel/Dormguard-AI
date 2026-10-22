"""Small SQL adapter: private SQLite by default, bounded PostgreSQL connections.

Writers serialize before reading state. This makes lifecycle/deduplication/outbox
and the audit chain one transaction across independent worker processes.
"""

import os
import sqlite3
import threading
import time
import weakref
from contextlib import ExitStack, contextmanager
from pathlib import Path

ACQUIRE_TIMEOUT_SECONDS = 10


class DatabaseUnavailable(Exception):
    """Sanitized bounded-acquisition failure; no SQL operation is replayed."""

    def __init__(self):
        super().__init__("Database temporarily unavailable; try again shortly")


class Connection:
    def __init__(self, raw, postgres=False):
        self.raw, self.postgres = raw, postgres

    def execute(self, sql, params=()):
        # All application statements are static; '?' only denotes parameters.
        return self.raw.execute(sql.replace("?", "%s") if self.postgres else sql, params)


class Database:
    def __init__(self, data_dir: Path, database_url=None):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self.path = self.data_dir / "dormguard.sqlite3"
        self.url = database_url
        self.postgres = bool(database_url)
        if database_url and not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("database_url must be a PostgreSQL URL")
        self.slots = threading.BoundedSemaphore(8)
        self._pool = None
        self._pool_lock = threading.Lock()
        self._closed = False
        self.initialize()

    def connect(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row

            return psycopg.connect(self.url, row_factory=dict_row, connect_timeout=10)
        conn = sqlite3.connect(self.path, timeout=30)
        os.chmod(self.path, 0o600)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _reset_postgres(raw):
        # Pool reset runs only after commit/rollback in IDLE state. Clear any
        # session-local settings/role without invalidating prepared statements.
        previous = raw.autocommit
        raw.autocommit = True
        try:
            raw.execute("RESET ROLE; RESET ALL", prepare=False)
        finally:
            raw.autocommit = previous

    @contextmanager
    def _slot(self):
        deadline = time.monotonic() + ACQUIRE_TIMEOUT_SECONDS
        if self._closed or not self.slots.acquire(timeout=ACQUIRE_TIMEOUT_SECONDS):
            raise DatabaseUnavailable()
        try:
            if self._closed or (remaining := deadline - time.monotonic()) <= 0:
                raise DatabaseUnavailable()
            yield remaining
        finally:
            self.slots.release()

    @contextmanager
    def _connection(self, *, timeout=ACQUIRE_TIMEOUT_SECONDS):
        if not self.postgres:
            raw = self.connect()
            try:
                yield raw
            finally:
                raw.close()
            return
        with self._pool_lock:
            if self._closed:
                raise DatabaseUnavailable()
            if self._pool is None:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool

                self._pool = ConnectionPool(
                    self.url,
                    kwargs={"row_factory": dict_row, "connect_timeout": 10},
                    min_size=1,
                    max_size=8,
                    timeout=ACQUIRE_TIMEOUT_SECONDS,
                    max_waiting=8,
                    max_idle=60,
                    max_lifetime=600,
                    reset=self._reset_postgres,
                    open=False,
                    name="dormguard-postgres",
                )
                weakref.finalize(self, self._pool.close)
                self._pool.open()
            pool = self._pool
        from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

        # Catch acquisition only. Exceptions from a borrowed transaction retain
        # their original meaning and uncertain writes are never retried.
        with ExitStack() as stack:
            try:
                raw = stack.enter_context(pool.connection(timeout=timeout))
            except (PoolTimeout, TooManyRequests, PoolClosed):
                raise DatabaseUnavailable() from None
            yield raw

    def close(self):
        """Stop this process's pool; borrowed connections close when returned."""
        with self._pool_lock:
            self._closed = True
            pool = self._pool
        if pool is not None:
            pool.close()

    @contextmanager
    def transaction(self, *, schema_upgrade=False):
        with self._slot() as timeout, self._connection(timeout=timeout) as raw:
            try:
                if self.postgres:
                    raw.execute("SELECT pg_advisory_xact_lock(1145528641)")
                else:
                    # SQLite's documented table-rebuild procedure. This is a
                    # dedicated connection; ordinary writers always enforce FKs.
                    if schema_upgrade:
                        raw.execute("PRAGMA foreign_keys=OFF")
                    raw.execute("BEGIN IMMEDIATE")
                yield Connection(raw, self.postgres)
                if schema_upgrade and not self.postgres:
                    if raw.execute("PRAGMA foreign_key_check").fetchone():
                        raise ValueError("Schema upgrade refused: foreign key violation")
                raw.commit()
            except BaseException:
                raw.rollback()
                raise

    @contextmanager
    def read(self):
        with self._slot() as timeout, self._connection(timeout=timeout) as raw:
            try:
                raw.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" if self.postgres else "BEGIN")
                yield Connection(raw, self.postgres)
            finally:
                raw.rollback()

    def initialize(self):
        root = Path(__file__).resolve().parent.parent / "migrations"
        if not self.postgres:
            raw = self.connect()
            try:
                raw.execute("PRAGMA journal_mode=WAL")
            finally:
                raw.close()
        with self.transaction(schema_upgrade=True) as conn:
            # Individual statements retain atomicity on SQLite (executescript commits).
            for sql in (root / "001_schema.sql").read_text().split(";"):
                if sql.strip():
                    conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(1,CURRENT_TIMESTAMP) ON CONFLICT(version) DO NOTHING"
            )
            if self.postgres:
                conn.raw.execute((root / "002_rls.sql").read_text())
            else:
                for table in ("audit_logs", "evidence"):
                    for operation in ("UPDATE", "DELETE"):
                        conn.execute(
                            f"CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'append-only record'); END"
                        )
            conn.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(2,CURRENT_TIMESTAMP) ON CONFLICT(version) DO NOTHING"
            )
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone():
                if self.postgres:
                    conn.raw.execute((root / "003_accounts_postgres.sql").read_text())
                else:
                    expected = {"id", "email", "role", "password_hash", "enabled", "created_at"}
                    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
                    custom = conn.execute(
                        "SELECT name FROM sqlite_master WHERE tbl_name='users' AND type IN ('index','trigger') AND sql IS NOT NULL"
                    ).fetchall()
                    if columns != expected or custom:
                        raise ValueError("Account upgrade requires review of the customized users schema")
                    for sql in (root / "003_accounts_sqlite.sql").read_text().split(";"):
                        if sql.strip():
                            conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(3,CURRENT_TIMESTAMP)")
