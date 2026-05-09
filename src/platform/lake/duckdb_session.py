# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DuckDB session pool for the lake infrastructure slice.

Concurrency model
-----------------
FastAPI handlers are async; DuckDB's ``DuckDBPyConnection`` is synchronous and
NOT safe to share across threads/coroutines. This module uses a ``queue.Queue``
(thread-safe, bounded) as the pool store. Async callers acquire via
``await asyncio.to_thread(factory.acquire)`` (see ``deps.py``). DuckDB queries
inside the session are blocking by nature; callers that need them inside an
async handler should also wrap with ``asyncio.to_thread``. This matches the
kernel's existing pattern for blocking drivers on hot paths.

Sessions are scoped to a single logical operation (one HTTP request, one
reconciliation run, one migration job). Never shared across concurrent
operations.
"""

import queue
import threading
from typing import Any

import duckdb
from src.platform.lake.config import LakeSettings
from src.platform.lake.exceptions import LakeSessionError, LakeSessionPoolExhaustedError
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields

_COMPONENT = 'platform.lake'


class LakeSession:
    """Wraps a single DuckDB connection.

    Use as a context manager — ``__exit__`` releases the connection back to the
    pool; it does NOT close it. Call ``close()`` only via the factory's
    ``close_all()``.

    ``warehouse_uri`` is the lake storage root (e.g. ``file:///path/to/wh`` or
    ``s3://bucket/wh``).  It is used by ``iceberg_table_path`` to build the
    full path for ``iceberg_scan`` calls.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        factory: 'LakeSessionFactory',
        warehouse_uri: str = '',
    ) -> None:
        self._conn = conn
        self._factory = factory
        self._warehouse_uri = warehouse_uri

    def iceberg_table_path(self, *namespace_and_table: str) -> str:
        """Return the full file-system path for ``iceberg_scan`` for a given table.

        Example::

            session.iceberg_table_path('raw', 'access_artifacts')
            # → '/path/to/wh/raw/access_artifacts'

        Strips the ``file://`` prefix for local storage.  For S3, returns the
        ``s3://…`` URI directly (DuckDB iceberg_scan accepts both).
        """
        path = '/'.join(namespace_and_table)
        uri = self._warehouse_uri.rstrip('/')
        if uri.startswith('file://'):
            return uri[len('file://') :] + '/' + path
        return uri + '/' + path

    def execute(self, sql: str, params: list[Any] | None = None) -> duckdb.DuckDBPyConnection:
        """Execute SQL and return the connection for chaining fetchone/fetchall."""
        if params is not None:
            return self._conn.execute(sql, params)
        return self._conn.execute(sql)

    def fetchone(self) -> Any:
        return self._conn.fetchone()

    def fetchall(self) -> list[Any]:
        return self._conn.fetchall()

    def fetch_arrow_table(self) -> Any:
        return self._conn.fetch_arrow_table()

    def close(self) -> None:
        """Close the underlying DuckDB connection. Called only by factory.close_all()."""
        self._conn.close()

    def __enter__(self) -> 'LakeSession':
        return self

    def __exit__(self, *_: Any) -> None:
        self._factory.release(self)


class LakeSessionFactory:
    """Bounded pool of DuckDB connections with iceberg + postgres extensions pre-loaded.

    Constructor args:
    - ``settings``: LakeSettings instance.
    - ``log_service``: injected LogService.
    - ``pg_dsn``: libpq-compatible DSN for Pattern-3 bootstrap (ATTACH + TEMP TABLE).
      Pass ``None`` to skip Pattern-3 (test-only affordance — documented below).

    Pattern-3 (cross-engine join policy — explicit Layer-1 exception)
    -----------------------------------------------------------------
    Defined in ``aurelion-mas/roadmap/phase_15.md`` Step 1 Key decisions,
    Pattern 3: small hot PG lookup tables are materialised once per session as a
    DuckDB TEMP TABLE. This avoids full-table scans on every cross-engine join.

    At connection open time (NOT on every acquire — the cost is paid once):
      1. ATTACH '<pg_dsn>' AS kernel_pg (TYPE postgres, READ_ONLY)
      2. CREATE TEMP TABLE ref_actions_local AS SELECT * FROM kernel_pg.ref_actions

    When ``pg_dsn`` is ``None`` the ATTACH + TEMP TABLE step is **skipped** and a
    DEBUG log ``platform.lake.ref_actions_skipped`` is emitted. This branch exists
    solely to allow unit tests to run without a live PostgreSQL instance (per
    Open Question O5 in phase_15.md). In production the lifespan always passes a
    real DSN. This is the single, documented test-only branch in this slice.
    """

    def __init__(
        self,
        settings: LakeSettings,
        log_service: LogService,
        pg_dsn: str | None = None,
    ) -> None:
        self._settings = settings
        self._log_service = log_service
        self._pg_dsn = pg_dsn
        self._pool: queue.Queue[duckdb.DuckDBPyConnection] = queue.Queue(maxsize=settings.pool_size)
        self._open_count: int = 0
        self._lock: threading.Lock = threading.Lock()

    def acquire(self) -> LakeSession:
        """Acquire a session from the pool.

        Concurrency model (load-bearing decision):
        ``queue.Queue`` is thread-safe and provides the blocking + timeout
        semantics needed. Async callers invoke this via
        ``await asyncio.to_thread(factory.acquire)`` (see ``deps.py``).

        Pattern-3 note: the cross-engine ATTACH + TEMP TABLE is performed ONCE
        per connection at open time, not on every acquire. This is intentional —
        opening is the one-time cost; subsequent acquires reuse the same
        bootstrapped connection. TEMP TABLEs are connection-scoped and dropped
        when the connection is closed, so there is no collision between sessions.

        See module docstring for the full cross-engine join policy and phase_15.md
        Pattern-3 documentation.
        """
        timeout = self._settings.acquire_timeout_seconds

        # Try to get from pool without waiting first.
        try:
            conn = self._pool.get_nowait()
            self._emit_acquired()
            return LakeSession(conn, self, warehouse_uri=self._settings.warehouse_uri)
        except queue.Empty:
            pass

        # No free slot — can we open a new connection?
        with self._lock:
            if self._open_count < self._settings.pool_size:
                conn = self._open_connection()
                self._open_count += 1
                self._emit_acquired()
                return LakeSession(conn, self, warehouse_uri=self._settings.warehouse_uri)

        # At capacity; block with timeout.
        try:
            conn = self._pool.get(timeout=timeout)
            self._emit_acquired()
            return LakeSession(conn, self, warehouse_uri=self._settings.warehouse_uri)
        except queue.Empty as exc:
            raise LakeSessionPoolExhaustedError(
                f'No DuckDB session available within {timeout}s (pool_size={self._settings.pool_size})'
            ) from exc

    def release(self, session: LakeSession) -> None:
        """Return the session's connection to the pool. Called by LakeSession.__exit__."""
        try:
            self._pool.put_nowait(session._conn)
        except queue.Full:
            # Pool is full (shouldn't happen in normal use); just close the connection.
            session.close()
            with self._lock:
                self._open_count -= 1

    def close_all(self) -> None:
        """Drain pool and close every connection. Called during lifespan shutdown."""
        closed = 0
        while True:
            try:
                conn = self._pool.get_nowait()
                conn.close()
                closed += 1
                with self._lock:
                    self._open_count -= 1
                self._log_service.emit_safe(
                    level=LogLevel.INFO,
                    message='platform.lake.session_closed',
                    component=_COMPONENT,
                    payload=merge_emit_log_participant_fields(
                        {'pool_open': self._open_count},
                        actor_component=_COMPONENT,
                        target_id='duckdb_session',
                    ),
                )
            except queue.Empty:
                break

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_connection(self) -> duckdb.DuckDBPyConnection:
        """Open a new DuckDB connection and bootstrap it (extensions + Pattern-3)."""
        conn = duckdb.connect()
        try:
            self._bootstrap(conn)
        except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
            conn.close()
            self._log_service.emit_safe(
                level=LogLevel.ERROR,
                message='platform.lake.session_bootstrap_failed',
                component=_COMPONENT,
                payload=merge_emit_log_participant_fields(
                    {'error': str(exc)},
                    actor_component=_COMPONENT,
                    target_id='duckdb_session',
                ),
            )
            raise LakeSessionError(f'DuckDB session bootstrap failed: {exc}') from exc
        return conn

    def _bootstrap(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Run one-time bootstrap SQL on a freshly opened connection."""
        conn.execute('INSTALL iceberg')
        conn.execute('LOAD iceberg')
        conn.execute('INSTALL postgres_scanner')
        conn.execute('LOAD postgres_scanner')
        # Enable version guessing so iceberg_scan can locate the latest metadata
        # without an explicit version-hint file.  This is safe in single-writer
        # kernel context (no concurrent uncommitted snapshots).
        conn.execute('SET unsafe_enable_version_guessing = true')

        if self._pg_dsn is None:
            # Test-only branch: skip Pattern-3 ATTACH + TEMP TABLE.
            self._log_service.emit_safe(
                level=LogLevel.DEBUG,
                message='platform.lake.ref_actions_skipped',
                component=_COMPONENT,
                payload=merge_emit_log_participant_fields(
                    {'reason': 'pg_dsn not provided (test mode)'},
                    actor_component=_COMPONENT,
                    target_id='duckdb_session',
                ),
            )
            return

        # Pattern-3: attach kernel PG and materialise ref_actions as a TEMP TABLE.
        conn.execute(f"ATTACH '{self._pg_dsn}' AS kernel_pg (TYPE postgres, READ_ONLY)")
        conn.execute('CREATE TEMP TABLE ref_actions_local AS SELECT * FROM kernel_pg.ref_actions')

    def _emit_acquired(self) -> None:
        self._log_service.emit_safe(
            level=LogLevel.INFO,
            message='platform.lake.session_acquired',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'pool_open': self._open_count,
                    'pool_capacity': self._settings.pool_size,
                },
                actor_component=_COMPONENT,
                target_id='duckdb_session',
            ),
        )
