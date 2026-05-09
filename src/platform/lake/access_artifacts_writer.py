# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Physical Iceberg write path for ``raw.access_artifacts``.

Thin domain-facing façade — physical I/O lives in platform/lake/access_artifacts_writer.py
(this module) and platform/lake/access_artifacts_reader.py.

Public entry points:
  - :func:`upsert_batch_iceberg` — two-snapshot dedup append + retire sequence.
  - :func:`tombstone_batch_iceberg` — in-place overwrite to mark rows as tombstoned.

Library module — MUST NOT call load_dotenv() / get_settings() at import time.
Forbidden imports: src.inventory.*, src.engines.*, src.products.*
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import traceback as _tb
from typing import Any, Literal, NoReturn
import uuid

from src.platform.lake.schemas import RAW_ACCESS_ARTIFACTS_TABLE
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import (
    LogService,
    NoOpLogService,
    merge_emit_log_participant_fields,
)

_COMPONENT = 'inventory.access_artifacts'


# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class AccessArtifactLakeWriteError(Exception):
    """Raised when an Iceberg write operation fails."""

    def __init__(self, message: str, *, cause: Exception) -> None:
        self.cause = cause
        super().__init__(message)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchUpsertResult:
    """Result of a batch upsert operation."""

    row_count: int
    snapshot_id: int | None
    backend: Literal['iceberg']


@dataclass(frozen=True, slots=True)
class BatchTombstoneResult:
    """Result of a batch tombstone operation."""

    row_count: int
    snapshot_id: int | None
    backend: Literal['iceberg']


# ---------------------------------------------------------------------------
# Input type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccessArtifactBatchItem:
    """Input item for upsert_batch_iceberg."""

    application_id: uuid.UUID
    artifact_type: str
    external_id: str
    payload: dict[str, Any]
    raw_name: str | None = None
    effect: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    observed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ts_micros(value: datetime | None) -> int | None:
    """Convert a datetime to microseconds since epoch for PyArrow timestamp arrays."""
    if value is None:
        return None
    ts = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(ts.timestamp() * 1_000_000)


def _extract_id_value(id_scalar: Any) -> Any:
    """Extract a comparable ID value from a PyArrow scalar."""
    return id_scalar.as_py()


def _build_arrow_table_for_upsert(
    items: list[AccessArtifactBatchItem],
    *,
    ingest_batch_id: uuid.UUID,
    pa_schema: Any,
) -> Any:
    """Build a PyArrow table from the input items for Iceberg append."""
    import pyarrow as pa  # noqa: PLC0415

    now = datetime.now(UTC)
    ts_type = pa.timestamp('us', tz='UTC')
    now_us = _ts_micros(now)
    batch_id_str = str(ingest_batch_id)

    row_ids: list[str] = []
    application_ids: list[str] = []
    artifact_types: list[str] = []
    external_ids: list[str] = []
    payloads: list[str | None] = []
    raw_names: list[str | None] = []
    effects: list[str | None] = []
    valid_froms: list[int | None] = []
    valid_untils: list[int | None] = []
    is_actives: list[bool] = []
    tombstoned_ats: list[int | None] = []
    observed_ats: list[int | None] = []
    ingested_ats: list[int | None] = []
    ingest_batch_ids: list[str] = []

    for item in items:
        row_ids.append(str(uuid.uuid4()))
        application_ids.append(str(item.application_id))
        artifact_types.append(item.artifact_type)
        external_ids.append(item.external_id)
        payloads.append(json.dumps(item.payload, sort_keys=True) if item.payload is not None else None)
        raw_names.append(item.raw_name)
        effects.append(item.effect)
        valid_froms.append(_ts_micros(item.valid_from))
        valid_untils.append(_ts_micros(item.valid_until))
        is_actives.append(True)
        tombstoned_ats.append(None)
        observed_ats.append(_ts_micros(item.observed_at) if item.observed_at is not None else now_us)
        ingested_ats.append(now_us)
        ingest_batch_ids.append(batch_id_str)

    raw: dict[str, Any] = {
        'id': pa.array(row_ids, type=pa.string()),
        'application_id': pa.array(application_ids, type=pa.string()),
        'artifact_type': pa.array(artifact_types, type=pa.string()),
        'external_id': pa.array(external_ids, type=pa.string()),
        'payload': pa.array(payloads, type=pa.string()),
        'raw_name': pa.array(raw_names, type=pa.string()),
        'effect': pa.array(effects, type=pa.string()),
        'valid_from': pa.array(valid_froms, type=ts_type),
        'valid_until': pa.array(valid_untils, type=ts_type),
        'is_active': pa.array(is_actives, type=pa.bool_()),
        'tombstoned_at': pa.array(tombstoned_ats, type=ts_type),
        'observed_at': pa.array(observed_ats, type=ts_type),
        'ingested_at': pa.array(ingested_ats, type=ts_type),
        'ingest_batch_id': pa.array(ingest_batch_ids, type=pa.string()),
    }

    return pa.table(raw, schema=pa_schema)


def _compute_retired_rows(
    scan_arrow: Any,
    *,
    input_keys: set[tuple[str, str]],
    new_ids: set[Any],
    observed_at: datetime,
) -> Any | None:
    """Identify existing active rows that are superseded by the new batch."""
    import pyarrow as pa  # noqa: PLC0415

    if len(scan_arrow) == 0:
        return None

    id_col = scan_arrow.column('id')
    is_active_col = scan_arrow.column('is_active')
    external_id_col = scan_arrow.column('external_id')
    application_id_col = scan_arrow.column('application_id')

    retiree_indices = []
    for i in range(len(scan_arrow)):
        row_active = is_active_col[i].as_py()
        if not row_active:
            continue
        row_id = _extract_id_value(id_col[i])
        if row_id in new_ids:
            continue
        row_external_id = external_id_col[i].as_py()
        row_app_id_raw = _extract_id_value(application_id_col[i])
        row_app_id_str = str(row_app_id_raw)
        if (row_external_id, row_app_id_str) in input_keys:
            retiree_indices.append(i)

    if not retiree_indices:
        return None

    retirees = scan_arrow.take(retiree_indices)

    tz_us = pa.timestamp('us', tz='UTC')
    ts_val = _ts_micros(observed_at)
    tombstoned_array = pa.array([ts_val] * len(retirees), type=tz_us)
    is_active_false = pa.array([False] * len(retirees), type=pa.bool_())

    col_names = retirees.column_names
    new_cols = []
    for name in col_names:
        if name == 'is_active':
            new_cols.append(is_active_false)
        elif name == 'tombstoned_at':
            new_cols.append(tombstoned_array)
        else:
            new_cols.append(retirees.column(name))

    return pa.table(dict(zip(col_names, new_cols, strict=True)), schema=retirees.schema)


def _scan_active_partition(
    table: Any,
    *,
    app_id_val: Any,
    artifact_type: str,
) -> Any:
    """Scan Iceberg table for active rows in a given partition."""
    from pyiceberg.expressions import And, EqualTo  # noqa: PLC0415

    eq_app: Any = EqualTo('application_id', app_id_val)  # type: ignore[misc, arg-type, call-arg]
    eq_type: Any = EqualTo('artifact_type', artifact_type)  # type: ignore[misc, arg-type, call-arg]
    eq_active: Any = EqualTo('is_active', True)  # type: ignore[misc, arg-type, call-arg]
    row_filter: Any = And(eq_app, And(eq_type, eq_active))
    return table.scan(row_filter=row_filter).to_arrow()


def _build_id_filter(ids: list[Any]) -> Any:
    """Build an Iceberg ``In`` filter for the ``id`` column."""
    from pyiceberg.expressions import In  # noqa: PLC0415

    return In('id', ids)  # type: ignore[misc, arg-type, call-arg]


def _translate_lake_write_error(
    exc: Exception,
    *,
    backend: str,
    operation: str,
    ingest_batch_id: uuid.UUID | None,
    log_service: LogService | NoOpLogService,
) -> NoReturn:
    """Wrap any Iceberg / PyArrow exception into AccessArtifactLakeWriteError."""
    print(f'\n[LAKE ERROR] op={operation} {type(exc).__name__}: {exc}', flush=True)
    _tb.print_exc()
    payload: dict[str, Any] = {
        'backend': backend,
        'operation': operation,
        'error_type': type(exc).__name__,
        'error_message': str(exc),
    }
    if ingest_batch_id is not None:
        payload['ingest_batch_id'] = str(ingest_batch_id)

    # allowed-emit-safe: provider boundary
    log_service.emit_safe(
        level=LogLevel.ERROR,
        message='inventory.access_artifacts.batch_write_failed',
        component=_COMPONENT,
        payload=merge_emit_log_participant_fields(
            payload,
            actor_component=_COMPONENT,
            target_id='batch',
        ),
    )
    raise AccessArtifactLakeWriteError(
        f'Lake write failed [{operation}]: {exc}',
        cause=exc,
    ) from exc


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def upsert_batch_iceberg(
    items: list[AccessArtifactBatchItem],
    *,
    ingest_batch_id: uuid.UUID,
    catalog: Any,
    log_service: LogService | NoOpLogService,
) -> BatchUpsertResult:
    """Write batch to Iceberg using two-snapshot dedup sequence.

    Args:
        items: Batch items to upsert.
        ingest_batch_id: Correlation ID for this ingest batch.
        catalog: PyIceberg catalog instance (caller owns lifecycle).
        log_service: For best-effort ERROR emission on write failure.

    Returns:
        :class:`BatchUpsertResult` with row count and latest snapshot ID.

    Raises:
        :class:`AccessArtifactLakeWriteError` on any Iceberg / PyArrow failure.
    """
    try:
        table = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        _translate_lake_write_error(
            exc,
            backend='iceberg',
            operation='load_table',
            ingest_batch_id=ingest_batch_id,
            log_service=log_service,
        )

    pa_schema = table.schema().as_arrow()

    try:
        arrow_new = _build_arrow_table_for_upsert(items, ingest_batch_id=ingest_batch_id, pa_schema=pa_schema)
    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        _translate_lake_write_error(
            exc,
            backend='iceberg',
            operation='build_arrow_table',
            ingest_batch_id=ingest_batch_id,
            log_service=log_service,
        )

    try:
        table.append(arrow_new)
    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        _translate_lake_write_error(
            exc,
            backend='iceberg',
            operation='append',
            ingest_batch_id=ingest_batch_id,
            log_service=log_service,
        )

    new_ids: set[Any] = set()
    id_col = arrow_new.column('id')
    for i in range(len(arrow_new)):
        new_ids.add(_extract_id_value(id_col[i]))

    partitions: list[tuple[Any, str]] = []
    seen_partitions: set[tuple[str, str]] = set()
    app_id_col = arrow_new.column('application_id')
    artifact_type_col = arrow_new.column('artifact_type')
    for i in range(len(arrow_new)):
        app_id_val = _extract_id_value(app_id_col[i])
        atype: str = artifact_type_col[i].as_py()
        app_id_str = str(app_id_val)
        key = (app_id_str, atype)
        if key not in seen_partitions:
            seen_partitions.add(key)
            partitions.append((app_id_val, atype))

    observed_at = datetime.now(UTC)

    input_keys: set[tuple[str, str]] = set()
    ext_id_col = arrow_new.column('external_id')
    for i in range(len(arrow_new)):
        app_id_val_i = _extract_id_value(app_id_col[i])
        ext_id: str = ext_id_col[i].as_py()
        input_keys.add((ext_id, str(app_id_val_i)))

    latest_snapshot_id: int | None = None
    try:
        latest_snapshot_id = table.metadata.current_snapshot_id
    except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
        pass

    for app_id_val, atype in partitions:
        app_id_str = str(app_id_val)
        try:
            scan_arrow = _scan_active_partition(table, app_id_val=app_id_val, artifact_type=atype)
        except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
            _translate_lake_write_error(
                exc,
                backend='iceberg',
                operation='scan_partition',
                ingest_batch_id=ingest_batch_id,
                log_service=log_service,
            )

        partition_input_keys = {(ext, aid) for (ext, aid) in input_keys if aid == app_id_str}

        retired = _compute_retired_rows(
            scan_arrow,
            input_keys=partition_input_keys,
            new_ids=new_ids,
            observed_at=observed_at,
        )
        if retired is None or len(retired) == 0:
            continue

        retiree_ids: list[Any] = []
        ret_id_col = retired.column('id')
        for i in range(len(retired)):
            retiree_ids.append(_extract_id_value(ret_id_col[i]))

        try:
            table.overwrite(
                retired,
                overwrite_filter=_build_id_filter(retiree_ids),
            )
            latest_snapshot_id = table.metadata.current_snapshot_id
        except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
            _translate_lake_write_error(
                exc,
                backend='iceberg',
                operation='overwrite_retire',
                ingest_batch_id=ingest_batch_id,
                log_service=log_service,
            )

    if latest_snapshot_id is None:
        try:
            latest_snapshot_id = table.metadata.current_snapshot_id
        except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
            pass

    return BatchUpsertResult(
        row_count=len(items),
        snapshot_id=latest_snapshot_id,
        backend='iceberg',
    )


async def tombstone_batch_iceberg(
    artifact_ids: list[uuid.UUID],
    *,
    observed_at: datetime,
    catalog: Any,
    log_service: LogService | NoOpLogService,
) -> BatchTombstoneResult:
    """Partition-level read-modify-write tombstone via Iceberg.

    Args:
        artifact_ids: IDs of artifacts to tombstone.
        observed_at: Timestamp to record as tombstoned_at.
        catalog: PyIceberg catalog instance (caller owns lifecycle).
        log_service: For best-effort ERROR emission on write failure.

    Returns:
        :class:`BatchTombstoneResult` with row count and snapshot ID.

    Raises:
        :class:`AccessArtifactLakeWriteError` on any Iceberg / PyArrow failure.
    """
    try:
        table = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        _translate_lake_write_error(
            exc,
            backend='iceberg',
            operation='load_table_tombstone',
            ingest_batch_id=None,
            log_service=log_service,
        )

    pa_schema = table.schema().as_arrow()
    id_type = pa_schema.field('id').type
    use_uuid_bytes = hasattr(id_type, 'wrap_array')

    if use_uuid_bytes:
        id_filter_values: list[Any] = [aid.bytes for aid in artifact_ids]
        id_values_set: set[Any] = {uuid.UUID(bytes=b) for b in id_filter_values}
    else:
        id_filter_values = [str(aid) for aid in artifact_ids]
        id_values_set = set(id_filter_values)

    try:
        scan_arrow = table.scan(
            row_filter=_build_id_filter(id_filter_values),
        ).to_arrow()
    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        _translate_lake_write_error(
            exc,
            backend='iceberg',
            operation='scan_tombstone',
            ingest_batch_id=None,
            log_service=log_service,
        )

    if len(scan_arrow) == 0:
        return BatchTombstoneResult(row_count=0, snapshot_id=None, backend='iceberg')

    import pyarrow as pa  # noqa: PLC0415

    tz_us = pa.timestamp('us', tz='UTC')
    ts_val = _ts_micros(observed_at)

    id_col = scan_arrow.column('id')
    match_mask = []
    for i in range(len(scan_arrow)):
        row_id = _extract_id_value(id_col[i])
        match_mask.append(row_id in id_values_set)

    tombstoned_vals = [ts_val if match_mask[i] else None for i in range(len(scan_arrow))]
    is_active_col_src = scan_arrow.column('is_active')
    is_active_vals = [False if match_mask[i] else is_active_col_src[i].as_py() for i in range(len(scan_arrow))]

    col_names = scan_arrow.column_names
    new_cols = []
    for name in col_names:
        if name == 'is_active':
            new_cols.append(pa.array(is_active_vals, type=pa.bool_()))
        elif name == 'tombstoned_at':
            new_cols.append(pa.array(tombstoned_vals, type=tz_us))
        else:
            new_cols.append(scan_arrow.column(name))

    modified_arrow = pa.table(dict(zip(col_names, new_cols, strict=True)), schema=scan_arrow.schema)

    try:
        table.overwrite(
            modified_arrow,
            overwrite_filter=_build_id_filter(id_filter_values),
        )
    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        _translate_lake_write_error(
            exc,
            backend='iceberg',
            operation='overwrite_tombstone',
            ingest_batch_id=None,
            log_service=log_service,
        )

    snapshot_id: int | None = None
    try:
        snapshot_id = table.metadata.current_snapshot_id
    except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
        pass

    return BatchTombstoneResult(
        row_count=len(artifact_ids),
        snapshot_id=snapshot_id,
        backend='iceberg',
    )
