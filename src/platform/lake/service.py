# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake service — read-only operations on the Iceberg catalog."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Literal
import urllib.parse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.lake.config import LakeSettings
from src.platform.lake.exceptions import LakeMaintenanceError  # noqa: F401 — re-exported for routes
from src.platform.lake.maintenance import (
    CleanOrphanFilesResult,
    CompactTableResult,
    ExpireSnapshotsResult,
    clean_orphan_files,
    compact_table,
    expire_old_snapshots,
)
from src.platform.lake.read_schemas import (
    LakeCompactionResponse,
    LakeStatusResponse,
    LakeTableCompactionResult,
    LakeTableStatus,
)
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog

_COMPONENT = 'platform.lake'
_CRED_STRIP_RE = re.compile(r':[^:@]+@')


def _redact_catalog_uri(uri: str) -> str:
    """Return *uri* with the password component removed.

    Uses :func:`urllib.parse.urlsplit` to strip credentials.  Falls back to a
    regex strip of ``:<password>@`` if URL parsing fails or does not round-trip
    cleanly (e.g. SQLAlchemy dialect URLs with query-string options).

    The guarantee: the substring ``:<password>@`` MUST NOT appear in the output.
    """
    try:
        parts = urllib.parse.urlsplit(uri)
        if parts.password is not None:
            host_part = parts.hostname or ''
            if parts.port:
                host_part = f'{host_part}:{parts.port}'
            netloc = f'{parts.username}@{host_part}'
            sanitised = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
            # Verify the password is gone; fall back to regex if not.
            if parts.password not in sanitised:
                return sanitised
    except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
        pass

    # Regex fallback: strip :<anything>@ from the authority section.
    return _CRED_STRIP_RE.sub('@', uri)


def get_lake_status(
    catalog: Catalog,
    settings: LakeSettings,
    *,
    log_service: LogService,
) -> LakeStatusResponse:
    """Return a snapshot of catalog and table state.

    Reads catalog metadata only — no Iceberg writes, no compaction, no PG mutations.
    Sorts tables by ``(namespace, name)`` for deterministic ordering.
    """
    tables: list[LakeTableStatus] = []

    for namespace_tuple in catalog.list_namespaces():
        for table_id in catalog.list_tables(namespace_tuple):
            tbl = catalog.load_table(table_id)
            parts: tuple[str, ...] = tbl.name()
            namespace = '.'.join(parts[:-1])
            name = parts[-1]

            snap = tbl.current_snapshot()
            last_updated_ms = snap.timestamp_ms if snap is not None else None

            tables.append(
                LakeTableStatus(
                    namespace=namespace,
                    name=name,
                    current_snapshot_id=tbl.metadata.current_snapshot_id,
                    snapshot_count=len(tbl.metadata.snapshots),
                    last_updated_ms=last_updated_ms,
                )
            )

    sorted_tables = sorted(tables, key=lambda t: (t.namespace, t.name))

    log_service.emit_safe(
        level=LogLevel.INFO,
        message='platform.lake.status_queried',
        component=_COMPONENT,
        payload=merge_emit_log_participant_fields(
            {'table_count': len(sorted_tables)},
            actor_component=_COMPONENT,
            target_id='catalog',
        ),
    )

    return LakeStatusResponse(
        catalog_uri=_redact_catalog_uri(settings.catalog_url),
        warehouse_uri=settings.warehouse_uri,
        storage_provider=settings.storage_provider,
        tables=sorted_tables,
    )


# ---------------------------------------------------------------------------
# Compaction helpers
# ---------------------------------------------------------------------------

_ALL_TABLES: list[tuple[str, str]] = [
    ('raw', 'access_artifacts'),
    ('normalized', 'access_facts'),
]


def _resolve_target_tables(
    target: Literal['raw.access_artifacts', 'normalized.access_facts', 'all'],
) -> list[tuple[str, str]]:
    """Return a list of ``(namespace, name)`` pairs for the given target."""
    if target == 'all':
        return list(_ALL_TABLES)
    ns, name = target.split('.', 1)
    return [(ns, name)]


async def _check_active_writes(
    session: AsyncSession,
    *,
    orphan_older_than_hours: int,
) -> str | None:
    """Return a skip reason string if active writes are detected, else ``None``.

    Queries:
    1. ``inventory_sync_runs`` for any row with ``status = 'running'``.
    2. ``lake_batches`` for rows created within ``2 × orphan_older_than_hours``.
    """
    result = await session.execute(text("SELECT COUNT(*) FROM inventory_sync_runs WHERE status = 'running'"))
    running_count: int = result.scalar_one()
    if running_count > 0:
        return "inventory_sync_run in 'running' state detected"

    window_hours = 2 * orphan_older_than_hours
    result2 = await session.execute(
        text(
            'SELECT COUNT(*) FROM lake_batches WHERE created_at > now() - make_interval(hours => :window_hours)'
        ).bindparams(window_hours=window_hours)
    )
    batch_count: int = result2.scalar_one()
    if batch_count > 0:
        return f'{batch_count} lake_batches created within last {window_hours}h'

    return None


async def run_compaction(
    catalog: Catalog,
    settings: LakeSettings,
    session: AsyncSession,
    *,
    table: Literal['raw.access_artifacts', 'normalized.access_facts', 'all'],
    retention_days: int,
    orphan_older_than_hours: int,
    target_file_size_mb: int,
    log_service: LogService,
) -> LakeCompactionResponse:
    """Run compaction, snapshot expiry, and optionally orphan cleanup for target tables.

    ``compact_table`` and ``expire_old_snapshots`` always run.
    ``clean_orphan_files`` is skipped when the safety gate detects active writes.
    """
    targets = _resolve_target_tables(table)
    skip_reason = await _check_active_writes(session, orphan_older_than_hours=orphan_older_than_hours)

    results: list[LakeTableCompactionResult] = []

    for ns, name in targets:
        iceberg_table = await asyncio.to_thread(catalog.load_table, (ns, name))

        compact_result: CompactTableResult = await asyncio.to_thread(
            compact_table,
            iceberg_table,
            target_file_size_mb=target_file_size_mb,
            log_service=log_service,
        )

        expire_result: ExpireSnapshotsResult = await asyncio.to_thread(
            expire_old_snapshots,
            iceberg_table,
            retention_days=retention_days,
            log_service=log_service,
        )

        clean_result: CleanOrphanFilesResult | None
        if skip_reason is None:
            clean_result = await asyncio.to_thread(
                clean_orphan_files,
                iceberg_table,
                older_than_hours=orphan_older_than_hours,
                log_service=log_service,
            )
        else:
            clean_result = None

        results.append(
            LakeTableCompactionResult(
                namespace=ns,
                name=name,
                files_before=compact_result.files_before,
                files_after=compact_result.files_after,
                bytes_before=compact_result.bytes_before,
                bytes_after=compact_result.bytes_after,
                compaction_snapshot_id=compact_result.snapshot_id,
                snapshots_removed=expire_result.snapshots_removed,
                latest_snapshot_id=expire_result.latest_snapshot_id,
                orphan_files_removed=clean_result.files_removed if clean_result is not None else 0,
                orphan_bytes_freed=clean_result.bytes_freed if clean_result is not None else 0,
                orphan_cleanup_skipped=skip_reason is not None,
                orphan_cleanup_skip_reason=skip_reason,
            )
        )

    orphan_skipped = skip_reason is not None

    log_service.emit_safe(
        level=LogLevel.INFO,
        message='platform.lake.compaction_run_completed',
        component=_COMPONENT,
        payload=merge_emit_log_participant_fields(
            {
                'tables_processed': len(results),
                'orphan_cleanup_skipped': orphan_skipped,
                'orphan_cleanup_skip_reason': skip_reason,
                'total_files_before': sum(r.files_before for r in results),
                'total_files_after': sum(r.files_after for r in results),
                'total_snapshots_removed': sum(r.snapshots_removed for r in results),
                'total_orphan_files_removed': sum(r.orphan_files_removed for r in results),
                'total_orphan_bytes_freed': sum(r.orphan_bytes_freed for r in results),
                'retention_days': retention_days,
                'orphan_older_than_hours': orphan_older_than_hours,
                'target_file_size_mb': target_file_size_mb,
            },
            actor_component=_COMPONENT,
            target_id='catalog',
        ),
    )

    return LakeCompactionResponse(
        tables=results,
        orphan_cleanup_skipped=orphan_skipped,
        orphan_cleanup_skip_reason=skip_reason,
    )
