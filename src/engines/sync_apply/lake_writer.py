# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Iceberg writer for ``normalized.access_facts`` — the **only** module allowed to write
that table.

Public entry points:

- :func:`write_run_batch` — group ``ReconciliationDeltaItem``s by operation, build one
  PyArrow table per non-empty bucket, commit at most 4 Iceberg snapshots.
- :func:`preflight_recover_already_written` — one DuckDB scan to find items already
  written to Iceberg (crash-recovery pattern).

Usage contract (caller-enforced):
    Caller MUST invoke preflight_recover_already_written before write_run_batch on the
    same item set. This module does not enforce idempotency at the writer level —
    Iceberg has no unique constraint on reconciliation_delta_item_id.

Concurrency:
    Assumes single-writer kernel. Preflight + append is not atomic across concurrent
    kernel processes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
import uuid

import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.expressions import In
from src.engines.reconciliation.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaOperation,
)
from src.platform.lake.duckdb_session import LakeSession
from src.platform.lake.schemas import (
    NORMALIZED_ACCESS_FACTS_TABLE,
    RAW_NORMALIZED_ACCESS_FACTS_SCHEMA_FIELDS,
)
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_component_trace_fields

_COMPONENT = 'engines.sync_apply'
_COMPONENT_ID = 'engines.sync_apply.lake_writer'

# Operations that produce Iceberg rows (noop is excluded from all Iceberg writes).
_WRITABLE_OPS: frozenset[ReconciliationDeltaOperation] = frozenset(
    {
        ReconciliationDeltaOperation.create,
        ReconciliationDeltaOperation.update,
        ReconciliationDeltaOperation.revoke,
        ReconciliationDeltaOperation.reactivate,
    }
)

# PyArrow type for timezone-aware microsecond timestamps.
_TS_TYPE: pa.DataType = pa.timestamp('us', tz='UTC')

# Ordered column names matching RAW_NORMALIZED_ACCESS_FACTS_SCHEMA_FIELDS.
_COLUMN_ORDER: tuple[str, ...] = tuple(f.name for f in RAW_NORMALIZED_ACCESS_FACTS_SCHEMA_FIELDS)

# Type alias for denorm_resolver argument.
DenormResolver = Callable[[ReconciliationDeltaItem], tuple[str, str]]

# Type alias for the per-operation bucketed mapping.
_OpBuckets = dict[ReconciliationDeltaOperation, list[ReconciliationDeltaItem]]


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class LakeWriterError(Exception):
    """Raised when lake_writer detects a pre-condition violation or type mismatch.

    Never raised for transient Iceberg I/O failures — those surface as the
    underlying PyIceberg exception (logged via emit_safe and then re-raised).
    """


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunWriteResult:
    """Per-operation row counts and Iceberg snapshot IDs from :func:`write_run_batch`.

    ``snapshot_ids`` is keyed by operation name (e.g. ``'create'``, ``'revoke'``).
    Absent key = empty bucket (no snapshot created).
    """

    create_count: int
    update_count: int
    revoke_count: int
    reactivate_count: int
    snapshot_ids: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreflightRecoveryResult:
    """IDs already written to Iceberg, returned by :func:`preflight_recover_already_written`."""

    recovered_ids: set[uuid.UUID]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bucket_items(items: Sequence[ReconciliationDeltaItem]) -> _OpBuckets:
    """Partition items into per-operation buckets; noop items are silently dropped."""
    buckets: _OpBuckets = {}
    for item in items:
        if item.operation not in _WRITABLE_OPS:
            continue
        buckets.setdefault(item.operation, []).append(item)
    return buckets


def _ts_micros_from_iso(value: str | None, *, field_name: str) -> int | None:
    """Parse an ISO-8601 timestamp string from JSON and return microseconds since epoch.

    Raises :class:`LakeWriterError` if the value is non-null but not timezone-aware.
    """
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise LakeWriterError(f'Field {field_name!r}: cannot parse timestamp {value!r}') from exc
    if dt.tzinfo is None:
        raise LakeWriterError(
            f'Field {field_name!r}: naive datetime {value!r} is not allowed; '
            'all timestamps must be timezone-aware (UTC).'
        )
    return int(dt.astimezone(UTC).timestamp() * 1_000_000)


def _ts_micros_from_datetime(dt: datetime | None, *, field_name: str) -> int | None:
    """Convert a Python ``datetime`` to microseconds since epoch.

    Raises :class:`LakeWriterError` for naive datetimes.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise LakeWriterError(
            f'Field {field_name!r}: naive datetime {dt!r} is not allowed; all timestamps must be timezone-aware (UTC).'
        )
    return int(dt.astimezone(UTC).timestamp() * 1_000_000)


def _uuid_str(value: uuid.UUID | None) -> str | None:
    if value is None:
        return None
    return str(value).lower()


def _resolve_json_source(
    item: ReconciliationDeltaItem,
) -> dict[str, Any]:
    """Return the JSON snapshot (after_json or before_json) appropriate for the operation.

    ``revoke`` → ``before_json``; all others → ``after_json``.
    """
    if item.operation == ReconciliationDeltaOperation.revoke:
        return dict(item.before_json or {})
    return dict(item.after_json or {})


def _validate_item(item: ReconciliationDeltaItem) -> None:
    """Validate pre-conditions for a single delta item before row construction.

    Raises :class:`LakeWriterError` on violation.
    """
    if not item.natural_key_hash:
        raise LakeWriterError(
            f'ReconciliationDeltaItem {item.id}: natural_key_hash is empty. '
            'The writer does not recompute hashes; the reconciliation pipeline must supply them.'
        )


def _is_uuid_extension(pa_type: pa.DataType) -> bool:
    """Return True if ``pa_type`` is the PyIceberg UUID extension type."""
    return hasattr(pa_type, 'wrap_array')


def _build_arrow_table(
    items: list[ReconciliationDeltaItem],
    *,
    denorm_resolver: DenormResolver,
    pa_schema: pa.Schema,
) -> pa.Table:
    """Build a PyArrow table compatible with an Iceberg table's PyArrow schema.

    Accepts the ``pa_schema`` obtained from ``pyiceberg.io.pyarrow.schema_to_pyarrow``
    on the target Iceberg table, so that:

    - UUID fields stored as ``extension<arrow.uuid>`` are wrapped via ``wrap_array``.
    - UUID fields stored as ``large_string`` (e.g. test tables using string partitions)
      are passed as canonical lowercase UUID strings.
    - Timestamps → ``pa.array(micros_since_epoch, type=timestamptz)``.
    - Booleans → ``pa.bool_()``.

    This adaptive approach allows the same writer code to work against both
    the production ``normalized.access_facts`` table (UUID partition) and the
    string-partition test fixtures used in the test suite (PyArrow 24 does not
    support ``group_by`` on ``extension<arrow.uuid>`` partition keys).

    Raises :class:`LakeWriterError` for:
    - empty ``natural_key_hash``
    - missing required fields in JSON snapshots
    - naive (non-UTC) datetime values
    """
    cols: dict[str, list[Any]] = {name: [] for name in _COLUMN_ORDER}

    for item in items:
        _validate_item(item)
        json_src = _resolve_json_source(item)
        app_id_denorm_str, subject_kind_denorm = denorm_resolver(item)

        # ---- fact-level identity fields (from ORM columns directly) ----
        # id: new UUID per Iceberg row (each write produces a new row identity).
        cols['id'].append(uuid.uuid4())
        cols['subject_id'].append(item.subject_id)
        cols['account_id'].append(item.account_id)
        cols['resource_id'].append(item.resource_id)
        # action_id is BIGINT in PG, STRING in Iceberg schema.
        cols['action_id'].append(str(item.action_id))

        # ---- fields from JSON snapshot ----
        effect = json_src.get('effect') or item.effect
        if not effect:
            raise LakeWriterError(f'ReconciliationDeltaItem {item.id}: missing required field "effect"')
        cols['effect'].append(effect)

        valid_from_raw = json_src.get('valid_from')
        valid_from_us = _ts_micros_from_iso(valid_from_raw, field_name='valid_from')
        cols['valid_from'].append(valid_from_us)

        cols['valid_until'].append(_ts_micros_from_iso(json_src.get('valid_until'), field_name='valid_until'))

        # is_active: revoke → False; create/update/reactivate → True
        cols['is_active'].append(item.operation != ReconciliationDeltaOperation.revoke)

        observed_at_raw = json_src.get('observed_at')
        observed_at_us = _ts_micros_from_iso(observed_at_raw, field_name='observed_at')
        if observed_at_us is None:
            # Fall back to item.created_at — access fact was observed at ingest time at the latest.
            observed_at_us = _ts_micros_from_datetime(item.created_at, field_name='observed_at')
        cols['observed_at'].append(observed_at_us)

        created_at_raw = json_src.get('created_at')
        created_at_us = _ts_micros_from_iso(created_at_raw, field_name='created_at')
        if created_at_us is None:
            # Fall back to item.created_at (ORM column, always present).
            created_at_us = _ts_micros_from_datetime(item.created_at, field_name='created_at')
        if created_at_us is None:
            raise LakeWriterError(f'ReconciliationDeltaItem {item.id}: missing required field "created_at"')
        cols['created_at'].append(created_at_us)

        cols['revoked_at'].append(_ts_micros_from_iso(json_src.get('revoked_at'), field_name='revoked_at'))

        # latest_batch_id: nullable UUID
        latest_batch_id_raw = json_src.get('latest_batch_id')
        if latest_batch_id_raw:
            try:
                cols['latest_batch_id'].append(uuid.UUID(latest_batch_id_raw))
            except (ValueError, AttributeError):
                cols['latest_batch_id'].append(None)
        else:
            cols['latest_batch_id'].append(None)

        # ---- denorm partition fields ----
        try:
            cols['application_id_denorm'].append(uuid.UUID(app_id_denorm_str))
        except (ValueError, AttributeError) as exc:
            raise LakeWriterError(
                f'denorm_resolver returned invalid UUID for application_id_denorm: {app_id_denorm_str!r}'
            ) from exc
        cols['subject_kind_denorm'].append(subject_kind_denorm)

        # ---- mandatory tracking fields ----
        cols['reconciliation_delta_item_id'].append(item.id)
        cols['natural_key_hash'].append(item.natural_key_hash)

    # Build PyArrow arrays adaptive to the target table's pa_schema.
    arrays: list[pa.Array] = []
    for name in _COLUMN_ORDER:
        data = cols[name]
        pa_type = pa_schema.field(name).type

        if name in ('valid_from', 'valid_until', 'observed_at', 'created_at', 'revoked_at'):
            arrays.append(pa.array(data, type=pa_type))
        elif name == 'is_active':
            arrays.append(pa.array(data, type=pa.bool_()))
        elif _is_uuid_extension(pa_type):
            # UUID extension type: convert uuid.UUID to bytes, then wrap.
            raw_bytes = [v.bytes if isinstance(v, uuid.UUID) else None for v in data]
            byte_array = pa.array(raw_bytes, type=pa.binary(16))
            arrays.append(pa_type.wrap_array(byte_array))
        else:
            # String/large_string: for UUID columns stored as string, convert to str.
            str_data = [str(v).lower() if isinstance(v, uuid.UUID) else v for v in data]
            arrays.append(pa.array(str_data, type=pa_type))

    return pa.table({name: arr for name, arr in zip(_COLUMN_ORDER, arrays)}, schema=pa_schema)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def write_run_batch(
    items: Sequence[ReconciliationDeltaItem],
    *,
    catalog: Catalog,
    denorm_resolver: DenormResolver,
    log_service: LogService,
) -> RunWriteResult:
    """Write a batch of ``ReconciliationDeltaItem``s to ``normalized.access_facts``.

    Groups items by operation (create / update / revoke / reactivate).
    Each non-empty bucket produces exactly ONE Iceberg snapshot; at most 4 snapshots
    total regardless of ``len(items)``.

    Empty buckets are silently skipped — no degenerate snapshots are created.
    ``noop`` items are silently skipped.

    Raises :class:`LakeWriterError` for:
    - empty ``items`` list (programming error — caller must filter)
    - item with empty ``natural_key_hash``
    - missing required JSON snapshot fields
    - naive (non-timezone-aware) datetime values

    Args:
        items: Sequence of delta items to write.
        catalog: PyIceberg catalog (injected by caller; lifecycle not managed here).
        denorm_resolver: Callable mapping each item to ``(application_id_denorm, subject_kind_denorm)``.
        log_service: For best-effort ERROR emission on commit failure.

    Returns:
        :class:`RunWriteResult` with per-operation counts and snapshot IDs.
    """
    if not items:
        raise LakeWriterError('write_run_batch called with empty items list. This is a programming error.')

    buckets = _bucket_items(items)

    iceberg_table = catalog.load_table(NORMALIZED_ACCESS_FACTS_TABLE)

    # Derive the PyArrow schema from the actual Iceberg table so the writer
    # adapts to UUID-extension fields (production) or large_string fields (test
    # string-partition fixtures) without hard-coding type assumptions.
    from pyiceberg.io.pyarrow import schema_to_pyarrow  # noqa: PLC0415

    pa_schema: pa.Schema = schema_to_pyarrow(iceberg_table.schema())

    snapshot_ids: dict[str, int] = {}
    create_count = 0
    update_count = 0
    revoke_count = 0
    reactivate_count = 0

    op_order = [
        ReconciliationDeltaOperation.create,
        ReconciliationDeltaOperation.update,
        ReconciliationDeltaOperation.revoke,
        ReconciliationDeltaOperation.reactivate,
    ]

    for op in op_order:
        bucket = buckets.get(op)
        if not bucket:
            continue

        arrow_table = _build_arrow_table(bucket, denorm_resolver=denorm_resolver, pa_schema=pa_schema)
        op_name = op.value
        hashes = [item.natural_key_hash for item in bucket]

        try:
            if op == ReconciliationDeltaOperation.create:
                iceberg_table.append(arrow_table)
            elif op in (
                ReconciliationDeltaOperation.update,
                ReconciliationDeltaOperation.reactivate,
            ):
                # Retire prior row(s) with same natural_key_hash and append new row.
                overwrite_filter: Any = In('natural_key_hash', hashes)  # type: ignore[misc, arg-type, call-arg]
                iceberg_table.overwrite(arrow_table, overwrite_filter=overwrite_filter)
            elif op == ReconciliationDeltaOperation.revoke:
                # In-place revoke: overwrite active rows with is_active=False rows.
                overwrite_filter = In('natural_key_hash', hashes)  # type: ignore[misc, arg-type, call-arg]
                iceberg_table.overwrite(arrow_table, overwrite_filter=overwrite_filter)

            # Reload after write to get the committed snapshot ID.
            committed_snapshot = catalog.load_table(NORMALIZED_ACCESS_FACTS_TABLE).current_snapshot()
            if committed_snapshot is not None:
                snapshot_ids[op_name] = committed_snapshot.snapshot_id
                # Refresh local table handle so next iteration sees updated state.
                iceberg_table = catalog.load_table(NORMALIZED_ACCESS_FACTS_TABLE)

        except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
            log_service.emit_safe(
                level=LogLevel.ERROR,
                message='platform.lake.fact_write_failed',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {
                        'operation': op_name,
                        'item_count': len(bucket),
                        'error': str(exc),
                        'error_type': type(exc).__name__,
                    },
                    component_id=_COMPONENT_ID,
                    target_id='normalized.access_facts',
                ),
            )
            raise

        if op == ReconciliationDeltaOperation.create:
            create_count = len(bucket)
        elif op == ReconciliationDeltaOperation.update:
            update_count = len(bucket)
        elif op == ReconciliationDeltaOperation.revoke:
            revoke_count = len(bucket)
        elif op == ReconciliationDeltaOperation.reactivate:
            reactivate_count = len(bucket)

    return RunWriteResult(
        create_count=create_count,
        update_count=update_count,
        revoke_count=revoke_count,
        reactivate_count=reactivate_count,
        snapshot_ids=snapshot_ids,
    )


def preflight_recover_already_written(
    items: Sequence[ReconciliationDeltaItem],
    *,
    lake_session: LakeSession,
) -> PreflightRecoveryResult:
    """Scan ``normalized.access_facts`` to find items already written in a prior run.

    Issues exactly ONE DuckDB query:
        SELECT DISTINCT reconciliation_delta_item_id
        FROM iceberg_scan('<path>')
        WHERE reconciliation_delta_item_id = ANY($1::varchar[])

    ``fetchall()`` is acceptable here — the result set is bounded to the size of one
    apply run batch (typically thousands of items, well within memory limits).

    Args:
        items: Pending delta items to check.
        lake_session: Active :class:`~src.platform.lake.duckdb_session.LakeSession`.

    Returns:
        :class:`PreflightRecoveryResult` with the set of already-written item IDs.
    """
    if not items:
        return PreflightRecoveryResult(recovered_ids=set())

    table_path = lake_session.iceberg_table_path('normalized', 'access_facts')
    item_ids: list[str] = [str(item.id) for item in items]

    sql = (
        f'SELECT DISTINCT reconciliation_delta_item_id '
        f"FROM iceberg_scan('{table_path}') "
        f'WHERE CAST(reconciliation_delta_item_id AS VARCHAR) = ANY($1::varchar[])'
    )
    lake_session.execute(sql, [item_ids])
    rows = lake_session.fetchall()

    recovered: set[uuid.UUID] = set()
    for row in rows:
        raw_val = row[0]
        if raw_val is not None:
            try:
                recovered.add(uuid.UUID(str(raw_val)))
            except (ValueError, AttributeError):
                pass

    return PreflightRecoveryResult(recovered_ids=recovered)
