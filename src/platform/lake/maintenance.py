# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Iceberg table maintenance helpers for the kernel data lake.

Public surface:
- ``compact_table(table, *, target_file_size_mb, log_service)`` — manual bin-pack via
  partition-level read/overwrite for partitions with many small files.
- ``expire_old_snapshots(table, *, retention_days, log_service)`` — removes snapshot
  metadata older than the retention window via PyIceberg 0.10+ maintenance API.
- ``clean_orphan_files(table, *, older_than_hours, log_service)`` — removes Parquet files
  under the table data directory that are not referenced by the current snapshot.

All three functions are idempotent and safe to call repeatedly. Caller owns the safety gate
against active writes (Step 17 endpoint enforces it).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
import urllib.parse

import fsspec
import pyarrow as pa
from pyiceberg.expressions import And, BooleanExpression, EqualTo
from src.platform.lake.exceptions import LakeMaintenanceError
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields

if TYPE_CHECKING:
    from pyiceberg.table import Table

_COMPONENT = 'platform.lake'

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompactTableResult:
    """Result of :func:`compact_table`."""

    files_before: int
    files_after: int
    bytes_before: int
    bytes_after: int
    snapshot_id: int | None


@dataclass(frozen=True, slots=True)
class ExpireSnapshotsResult:
    """Result of :func:`expire_old_snapshots`."""

    snapshots_removed: int
    latest_snapshot_id: int | None


@dataclass(frozen=True, slots=True)
class CleanOrphanFilesResult:
    """Result of :func:`clean_orphan_files`."""

    files_removed: int
    bytes_freed: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_name_parts(table: Table) -> tuple[str, str]:
    """Return ``(namespace_dot_str, table_name)`` from the table identifier.

    ``Table.name()`` returns an ``Identifier`` which is ``tuple[str, ...]``.
    """
    parts: tuple[str, ...] = table.name()
    namespace = '.'.join(parts[:-1])
    name = parts[-1]
    return namespace, name


def _target_id(namespace: str, name: str) -> str:
    return f'{namespace}.{name}' if namespace else name


# ---------------------------------------------------------------------------
# compact_table
# ---------------------------------------------------------------------------


def compact_table(
    table: Table,
    *,
    target_file_size_mb: int = 128,
    log_service: LogService,
) -> CompactTableResult:
    """Bin-pack small Parquet files within each partition via read/overwrite.

    For each partition that contains two or more data files where the file size is
    below ``target_file_size_mb * 0.5`` MB, the partition is read into a PyArrow
    table and rewritten as a single file via ``table.overwrite(...)``.

    Partitions that are already compact (no small-file candidates) are left
    untouched — this is the idempotency guarantee.

    Caller owns the safety gate against active writes (Step 17 endpoint enforces it).
    """
    namespace, name = _table_name_parts(table)

    try:
        # Idempotent on empty tables.
        if table.current_snapshot() is None:
            return CompactTableResult(
                files_before=0,
                files_after=0,
                bytes_before=0,
                bytes_after=0,
                snapshot_id=None,
            )

        files_table: pa.Table = table.inspect.files()
        rows = files_table.to_pylist()

        files_before = len(rows)
        bytes_before = sum(int(r['file_size_in_bytes']) for r in rows)

        threshold_bytes = target_file_size_mb * 1024 * 1024 * 0.5

        # Group files by partition tuple (convert Struct scalar → hashable frozenset of items).
        partition_files: dict[tuple[tuple[str, Any], ...], list[dict[str, Any]]] = {}
        for row in rows:
            part = row['partition']
            # PyArrow StructScalar: iterate over its keys/values.
            try:
                key = tuple(sorted((k, part[k].as_py()) for k in part.keys()))
            except AttributeError:
                # Fallback for plain dict-like partition representation.
                key = tuple(sorted((k, v) for k, v in (part.items() if hasattr(part, 'items') else [])))
            partition_files.setdefault(key, []).append(row)

        compacted_any = False
        for part_key, part_rows in partition_files.items():
            small = [r for r in part_rows if int(r['file_size_in_bytes']) < threshold_bytes]
            if len(small) < 2:
                continue

            # Build equality filter for this partition.
            filter_expr = _build_partition_filter(part_key)

            arrow = table.scan(row_filter=filter_expr).to_arrow()
            table.overwrite(arrow, overwrite_filter=filter_expr)
            compacted_any = True

        table.refresh()
        files_after_table: pa.Table = table.inspect.files()
        after_rows = files_after_table.to_pylist()
        files_after = len(after_rows)
        bytes_after = sum(int(r['file_size_in_bytes']) for r in after_rows)
        snapshot = table.current_snapshot()
        snapshot_id = snapshot.snapshot_id if snapshot is not None else None

        if not compacted_any:
            # No rewrite — return current state unchanged.
            files_after = files_before
            bytes_after = bytes_before

    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        log_service.emit_safe(
            level=LogLevel.ERROR,
            message='platform.lake.compaction_failed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'namespace': namespace,
                    'table': name,
                    'error': str(exc),
                    'error_type': type(exc).__name__,
                },
                actor_component=_COMPONENT,
                target_id=_target_id(namespace, name),
            ),
        )
        raise LakeMaintenanceError(f'Compaction failed for {namespace}.{name}: {exc}') from exc

    log_service.emit_safe(
        level=LogLevel.INFO,
        message='platform.lake.compaction_completed',
        component=_COMPONENT,
        payload=merge_emit_log_participant_fields(
            {
                'namespace': namespace,
                'table': name,
                'files_before': files_before,
                'files_after': files_after,
                'bytes_before': bytes_before,
                'bytes_after': bytes_after,
                'snapshot_id': snapshot_id,
                'target_file_size_mb': target_file_size_mb,
            },
            actor_component=_COMPONENT,
            target_id=_target_id(namespace, name),
        ),
    )

    return CompactTableResult(
        files_before=files_before,
        files_after=files_after,
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        snapshot_id=snapshot_id,
    )


def _build_partition_filter(part_key: tuple[tuple[str, Any], ...]) -> BooleanExpression:
    """Build a PyIceberg BooleanExpression from a partition key tuple.

    Returns an equality expression (possibly an And of multiple equalities).
    Falls back to AlwaysTrue if the partition has no fields.
    """
    from pyiceberg.expressions import AlwaysTrue

    exprs: list[BooleanExpression] = []
    for field_name, value in part_key:
        if value is not None:
            exprs.append(EqualTo(field_name, literal=value))  # type: ignore[call-arg, misc, arg-type]

    if not exprs:
        return AlwaysTrue()
    result: BooleanExpression = exprs[0]
    for e in exprs[1:]:
        result = And(result, e)
    return result


# ---------------------------------------------------------------------------
# expire_old_snapshots
# ---------------------------------------------------------------------------


def expire_old_snapshots(
    table: Table,
    *,
    retention_days: int = 7,
    log_service: LogService,
) -> ExpireSnapshotsResult:
    """Expire snapshot metadata older than ``retention_days`` using PyIceberg 0.10+ API.

    Uses ``table.maintenance.expire_snapshots().older_than(cutoff_dt).commit()``.

    Idempotent: if no snapshots predate the cutoff, the commit is a no-op and
    ``snapshots_removed == 0``.

    Caller owns the safety gate against active writes (Step 17 endpoint enforces it).
    """
    namespace, name = _table_name_parts(table)

    try:
        before_count = len(table.metadata.snapshots)
        cutoff_dt = datetime.now(tz=UTC) - timedelta(days=retention_days)

        table.maintenance.expire_snapshots().older_than(cutoff_dt).commit()

        table.refresh()
        after_count = len(table.metadata.snapshots)
        snapshots_removed = max(0, before_count - after_count)

        current = table.current_snapshot()
        latest_snapshot_id = current.snapshot_id if current is not None else None

    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        log_service.emit_safe(
            level=LogLevel.ERROR,
            message='platform.lake.snapshots_expire_failed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'namespace': namespace,
                    'table': name,
                    'error': str(exc),
                    'error_type': type(exc).__name__,
                },
                actor_component=_COMPONENT,
                target_id=_target_id(namespace, name),
            ),
        )
        raise LakeMaintenanceError(f'Snapshot expiry failed for {namespace}.{name}: {exc}') from exc

    log_service.emit_safe(
        level=LogLevel.INFO,
        message='platform.lake.snapshots_expired',
        component=_COMPONENT,
        payload=merge_emit_log_participant_fields(
            {
                'namespace': namespace,
                'table': name,
                'snapshots_removed': snapshots_removed,
                'latest_snapshot_id': latest_snapshot_id,
                'retention_days': retention_days,
            },
            actor_component=_COMPONENT,
            target_id=_target_id(namespace, name),
        ),
    )

    return ExpireSnapshotsResult(
        snapshots_removed=snapshots_removed,
        latest_snapshot_id=latest_snapshot_id,
    )


# ---------------------------------------------------------------------------
# clean_orphan_files
# ---------------------------------------------------------------------------


def clean_orphan_files(
    table: Table,
    *,
    older_than_hours: int = 24,
    log_service: LogService,
) -> CleanOrphanFilesResult:
    """Remove Parquet files under the table data directory not referenced by the current snapshot.

    **Design choice:** referenced files are derived from the *current* snapshot only
    (via ``table.inspect.files()``). Expired snapshots' data files are handled by
    ``expire_old_snapshots``, which is the partner operation. This function only
    reclaims files that were written to storage but never committed to any snapshot
    (partial-write crash artifacts).

    The ``older_than_hours`` guard avoids racing against in-flight writes — files
    younger than the guard are left on disk even if unreferenced.

    Uses ``fsspec`` to walk the data directory and delete orphan files. The URI scheme
    is derived from ``table.location()`` so both ``file://`` (dev) and ``s3://`` (prod)
    paths work transparently.

    Caller owns the safety gate against active writes (Step 17 endpoint enforces it).
    """
    namespace, name = _table_name_parts(table)

    try:
        # Build the set of file paths referenced by the current snapshot.
        if table.current_snapshot() is not None:
            files_table: pa.Table = table.inspect.files()
            referenced: set[str] = {row['file_path'] for row in files_table.to_pylist()}
        else:
            referenced = set()

        location = table.location()
        # Strip trailing slash for consistent path construction.
        location = location.rstrip('/')
        data_dir = f'{location}/data'

        scheme = urllib.parse.urlparse(data_dir).scheme or 'file'
        # fsspec uses 'file' for local paths; strip scheme prefix for local listings.
        fs = fsspec.filesystem(scheme)

        # Normalise data_dir to the path component fsspec understands.
        if scheme == 'file':
            # fsspec local fs expects POSIX path without 'file://' prefix.
            parsed = urllib.parse.urlparse(data_dir)
            local_data_dir = parsed.path
        else:
            local_data_dir = data_dir

        # Check if data dir exists; if not, nothing to clean.
        if not fs.exists(local_data_dir):
            log_service.emit_safe(
                level=LogLevel.INFO,
                message='platform.lake.orphan_files_cleaned',
                component=_COMPONENT,
                payload=merge_emit_log_participant_fields(
                    {
                        'namespace': namespace,
                        'table': name,
                        'files_removed': 0,
                        'bytes_freed': 0,
                        'older_than_hours': older_than_hours,
                    },
                    actor_component=_COMPONENT,
                    target_id=_target_id(namespace, name),
                ),
            )
            return CleanOrphanFilesResult(files_removed=0, bytes_freed=0)

        cutoff_ts = (datetime.now(tz=UTC) - timedelta(hours=older_than_hours)).timestamp()

        files_removed = 0
        bytes_freed = 0

        # Walk directory recursively; fsspec find() returns a flat list.
        all_files: list[str] = fs.find(local_data_dir)
        for fpath in all_files:
            # Reconstruct the full URI as PyIceberg stores it.
            if scheme == 'file':
                full_uri = f'file://{fpath}'
            else:
                full_uri = fpath

            # Skip files that are referenced by the current snapshot.
            if full_uri in referenced:
                continue

            info = fs.info(fpath)
            mtime = info.get('mtime') or info.get('LastModified')
            if mtime is None:
                continue

            # Normalise mtime to a Unix timestamp float.
            if isinstance(mtime, datetime):
                mtime_ts = mtime.timestamp()
            else:
                mtime_ts = float(mtime)

            if mtime_ts >= cutoff_ts:
                # Within the safety window — do not delete.
                continue

            file_size = int(info.get('size', 0))
            fs.rm(fpath)
            files_removed += 1
            bytes_freed += file_size

    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        log_service.emit_safe(
            level=LogLevel.ERROR,
            message='platform.lake.orphan_files_clean_failed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'namespace': namespace,
                    'table': name,
                    'error': str(exc),
                    'error_type': type(exc).__name__,
                },
                actor_component=_COMPONENT,
                target_id=_target_id(namespace, name),
            ),
        )
        raise LakeMaintenanceError(f'Orphan file cleanup failed for {namespace}.{name}: {exc}') from exc

    log_service.emit_safe(
        level=LogLevel.INFO,
        message='platform.lake.orphan_files_cleaned',
        component=_COMPONENT,
        payload=merge_emit_log_participant_fields(
            {
                'namespace': namespace,
                'table': name,
                'files_removed': files_removed,
                'bytes_freed': bytes_freed,
                'older_than_hours': older_than_hours,
            },
            actor_component=_COMPONENT,
            target_id=_target_id(namespace, name),
        ),
    )

    return CleanOrphanFilesResult(files_removed=files_removed, bytes_freed=bytes_freed)
