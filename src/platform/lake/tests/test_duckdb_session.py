# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for src/platform/lake/duckdb_session.py."""

import os
import threading

import pytest
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSessionFactory
from src.platform.lake.exceptions import LakeSessionPoolExhaustedError
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink


def make_factory(
    pool_size: int = 2,
    acquire_timeout: float = 5.0,
    pg_dsn: str | None = None,
) -> tuple[LakeSessionFactory, CapturingLogSink]:
    sink = CapturingLogSink()
    log_service = LogService(sink=sink)
    settings = LakeSettings(
        catalog_url='sqlite:///test_catalog.db',
        warehouse_uri='file:///tmp/test_warehouse',
        pool_size=pool_size,
        acquire_timeout_seconds=acquire_timeout,
    )
    factory = LakeSessionFactory(settings=settings, log_service=log_service, pg_dsn=pg_dsn)
    return factory, sink


def test_select_42() -> None:
    factory, _ = make_factory()
    with factory.acquire() as session:
        row = session.execute('SELECT 42').fetchone()
    assert row == (42,)
    factory.close_all()


def test_iceberg_and_postgres_extensions_loaded() -> None:
    factory, _ = make_factory()
    with factory.acquire() as session:
        sql = (
            'SELECT extension_name, loaded FROM duckdb_extensions() '
            "WHERE extension_name IN ('iceberg', 'postgres_scanner')"
        )
        rows = session.execute(sql).fetchall()
    loaded = {r[0]: r[1] for r in rows}
    assert loaded.get('iceberg') is True
    assert loaded.get('postgres_scanner') is True
    factory.close_all()


def test_pool_exhaustion() -> None:
    factory, _ = make_factory(pool_size=1, acquire_timeout=0.1)
    session1 = factory.acquire()
    # Do NOT release session1; second acquire should fail.
    with pytest.raises(LakeSessionPoolExhaustedError):
        factory.acquire()
    # Cleanup: release and close.
    session1.__exit__(None, None, None)
    factory.close_all()


def test_close_all_resets_open_count() -> None:
    factory, _ = make_factory(pool_size=2)
    with factory.acquire() as _:
        pass
    assert factory._open_count == 1
    factory.close_all()
    assert factory._open_count == 0

    # After close_all, acquire opens a new connection.
    with factory.acquire() as session:
        row = session.execute('SELECT 1').fetchone()
    assert row == (1,)
    assert factory._open_count == 1
    factory.close_all()


def test_log_emission() -> None:
    factory, sink = make_factory()
    with factory.acquire() as _:
        pass
    factory.close_all()

    # emit_safe runs asyncio.run() synchronously (no running loop),
    # so records are captured immediately after each call.
    acquired = [r for r in sink.records if 'session_acquired' in r.message]
    closed = [r for r in sink.records if 'session_closed' in r.message]
    assert len(acquired) >= 1
    assert len(closed) >= 1


@pytest.mark.skipif(
    not os.environ.get('KERNEL_PG_DSN'),
    reason='Requires KERNEL_PG_DSN env to be set for integration test',
)
def test_ref_actions_local_with_real_pg() -> None:
    pg_dsn = os.environ['KERNEL_PG_DSN']
    factory, _ = make_factory(pg_dsn=pg_dsn)
    with factory.acquire() as session:
        row = session.execute('SELECT count(*) FROM ref_actions_local').fetchone()
    assert row is not None and row[0] >= 0
    factory.close_all()


def test_acquire_release_concurrent() -> None:
    """Sessions released by one thread become available to another."""
    factory, _ = make_factory(pool_size=1, acquire_timeout=2.0)
    results: list[int] = []

    def worker() -> None:
        with factory.acquire() as session:
            row = session.execute('SELECT 100').fetchone()
            if row:
                results.append(row[0])

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t1.join()
    t2.start()
    t2.join()

    assert results == [100, 100]
    factory.close_all()
