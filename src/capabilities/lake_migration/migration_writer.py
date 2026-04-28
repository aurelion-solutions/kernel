# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Migration writer helper for PG → Iceberg identity-preserving writes.

Design contract (D1 — Phase 15 Step 14 architect decision):
  Direct PyIceberg ``table.append()`` — NOT via ``sync_apply.lake_writer.write_run_batch``.
  Reason: ``write_run_batch`` mints new UUIDs (``uuid.uuid4()`` for each row), which
  would destroy the identity-preservation requirement.

Public API (pure functions — no logging, no DI):
  - ``build_artifact_arrow_table(rows, *, pa_schema) -> pa.Table``
  - ``build_fact_arrow_table(rows, *, pa_schema, denorm_map, delta_item_id_map) -> pa.Table``
  - ``append_artifact_batch(catalog, pa_table) -> int``
  - ``append_fact_batch(catalog, pa_table) -> int``

No LogService calls inside this module — logging stays in service.py per the
"only services emit" architectural rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import TYPE_CHECKING, Any
import uuid

import pyarrow as pa
from pyiceberg.catalog import Catalog
from src.platform.lake.schemas import (
    NORMALIZED_ACCESS_FACTS_TABLE,
    RAW_ACCESS_ARTIFACTS_TABLE,
    RAW_NORMALIZED_ACCESS_FACTS_SCHEMA_FIELDS,
)

if TYPE_CHECKING:
    pass

# Column names for each table (ordered)
_ARTIFACT_COLUMNS: tuple[str, ...] = (
    'id',
    'application_id',
    'artifact_type',
    'external_id',
    'payload',
    'raw_name',
    'effect',
    'valid_from',
    'valid_until',
    'is_active',
    'tombstoned_at',
    'observed_at',
    'ingested_at',
    'ingest_batch_id',
)

_FACT_COLUMNS: tuple[str, ...] = tuple(f.name for f in RAW_NORMALIZED_ACCESS_FACTS_SCHEMA_FIELDS)

_TS_TYPE = pa.timestamp('us', tz='UTC')


def _is_uuid_extension(pa_type: pa.DataType) -> bool:
    """Return True if ``pa_type`` is the PyIceberg UUID extension type."""
    return hasattr(pa_type, 'wrap_array')


def _dt_to_us(dt: datetime | None) -> int | None:
    """Convert a datetime to microseconds since epoch (UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.astimezone(UTC).timestamp() * 1_000_000)


def _build_arrays(
    col_data: dict[str, list[Any]],
    col_order: tuple[str, ...],
    pa_schema: pa.Schema,
) -> list[pa.Array]:
    """Convert column data dict to ordered PyArrow arrays respecting UUID extension types."""
    arrays: list[pa.Array] = []
    for name in col_order:
        data = col_data[name]
        pa_type = pa_schema.field(name).type

        if name.endswith('_at') or name in ('valid_from', 'valid_until'):
            arrays.append(pa.array(data, type=pa_type))
        elif name == 'is_active':
            arrays.append(pa.array(data, type=pa.bool_()))
        elif _is_uuid_extension(pa_type):
            raw_bytes = [v.bytes if isinstance(v, uuid.UUID) else None for v in data]
            byte_arr = pa.array(raw_bytes, type=pa.binary(16))
            arrays.append(pa_type.wrap_array(byte_arr))
        else:
            str_data = [str(v).lower() if isinstance(v, uuid.UUID) else v for v in data]
            arrays.append(pa.array(str_data, type=pa_type))
    return arrays


def build_artifact_arrow_table(
    rows: list[Any],
    *,
    pa_schema: pa.Schema,
) -> pa.Table:
    """Build a PyArrow table from PG access_artifact ORM rows.

    Preserves ALL identity/lifecycle columns (id, created_at, observed_at,
    valid_from, valid_until) — NO new UUIDs minted.

    ``payload`` is a JSONB dict in PG; stored as JSON string in Iceberg
    (``payload`` column is StringType in the lake schema).

    Args:
        rows:       List of SQLAlchemy ORM instances (access_artifacts).
        pa_schema:  PyArrow schema from the target Iceberg table.

    Returns:
        PyArrow table matching ``pa_schema``.
    """
    cols: dict[str, list[Any]] = {name: [] for name in _ARTIFACT_COLUMNS}

    for row in rows:
        cols['id'].append(row.id)
        cols['application_id'].append(row.application_id)
        cols['artifact_type'].append(row.artifact_type)
        cols['external_id'].append(row.external_id)
        # payload: JSONB → JSON string
        payload_val = row.payload if row.payload is not None else None
        if isinstance(payload_val, dict):
            cols['payload'].append(json.dumps(payload_val))
        elif payload_val is not None:
            cols['payload'].append(str(payload_val))
        else:
            cols['payload'].append(None)
        cols['raw_name'].append(getattr(row, 'raw_name', None))
        cols['effect'].append(getattr(row, 'effect', None))
        cols['valid_from'].append(_dt_to_us(getattr(row, 'valid_from', None)))
        cols['valid_until'].append(_dt_to_us(getattr(row, 'valid_until', None)))
        cols['is_active'].append(bool(row.is_active))
        cols['tombstoned_at'].append(_dt_to_us(getattr(row, 'tombstoned_at', None)))
        cols['observed_at'].append(_dt_to_us(row.observed_at))
        cols['ingested_at'].append(_dt_to_us(getattr(row, 'ingested_at', None) or row.observed_at))
        cols['ingest_batch_id'].append(getattr(row, 'ingest_batch_id', None))

    arrays = _build_arrays(cols, _ARTIFACT_COLUMNS, pa_schema)
    return pa.table(
        {name: arr for name, arr in zip(_ARTIFACT_COLUMNS, arrays)},
        schema=pa_schema,
    )


def build_fact_arrow_table(
    rows: list[Any],
    *,
    pa_schema: pa.Schema,
    denorm_map: dict[uuid.UUID, tuple[uuid.UUID, str]],
    delta_item_id_map: dict[uuid.UUID, uuid.UUID],
    natural_key_hash_map: dict[uuid.UUID, str],
) -> pa.Table:
    """Build a PyArrow table from PG access_fact ORM rows.

    Preserves ALL identity columns.  Carries ``reconciliation_delta_item_id``
    and ``natural_key_hash`` from the provided maps.

    Args:
        rows:                 List of SQLAlchemy ORM instances (access_facts).
        pa_schema:            PyArrow schema from the target Iceberg table.
        denorm_map:           ``fact.id → (application_id_denorm, subject_kind_denorm)``.
        delta_item_id_map:    ``fact.id → reconciliation_delta_item_id``.
        natural_key_hash_map: ``fact.id → natural_key_hash`` (64-char hex).

    Returns:
        PyArrow table matching ``pa_schema``.
    """
    cols: dict[str, list[Any]] = {name: [] for name in _FACT_COLUMNS}

    for row in rows:
        fact_id: uuid.UUID = row.id
        app_id_denorm, subject_kind_denorm = denorm_map.get(fact_id, (None, None))
        delta_item_id = delta_item_id_map.get(fact_id)
        nk_hash = natural_key_hash_map.get(fact_id)

        cols['id'].append(fact_id)
        cols['subject_id'].append(row.subject_id)
        cols['account_id'].append(getattr(row, 'account_id', None))
        cols['resource_id'].append(row.resource_id)
        # action_id: BIGINT in PG, STRING in Iceberg schema
        cols['action_id'].append(str(row.action_id))
        effect_val = row.effect
        if hasattr(effect_val, 'value'):
            effect_val = effect_val.value
        cols['effect'].append(str(effect_val) if effect_val is not None else None)
        cols['valid_from'].append(_dt_to_us(row.valid_from))
        cols['valid_until'].append(_dt_to_us(getattr(row, 'valid_until', None)))
        cols['is_active'].append(bool(row.is_active))
        cols['observed_at'].append(_dt_to_us(row.observed_at))
        cols['created_at'].append(_dt_to_us(row.created_at))
        cols['revoked_at'].append(_dt_to_us(getattr(row, 'revoked_at', None)))
        cols['latest_batch_id'].append(getattr(row, 'latest_batch_id', None))
        cols['application_id_denorm'].append(app_id_denorm)
        cols['subject_kind_denorm'].append(subject_kind_denorm)
        cols['reconciliation_delta_item_id'].append(delta_item_id)
        cols['natural_key_hash'].append(nk_hash)

    arrays = _build_arrays(cols, _FACT_COLUMNS, pa_schema)
    return pa.table(
        {name: arr for name, arr in zip(_FACT_COLUMNS, arrays)},
        schema=pa_schema,
    )


def append_artifact_batch(catalog: Catalog, pa_table: pa.Table) -> int:
    """Append ``pa_table`` to ``raw.access_artifacts`` and return the snapshot id.

    Calls ``table.append()`` once — ONE Iceberg snapshot per batch.

    Returns:
        Snapshot id from ``current_snapshot()``.
    """
    iceberg_table = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
    iceberg_table.append(pa_table)
    # Reload to get the committed snapshot id.
    committed = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE).current_snapshot()
    return committed.snapshot_id if committed is not None else -1


def append_fact_batch(catalog: Catalog, pa_table: pa.Table) -> int:
    """Append ``pa_table`` to ``normalized.access_facts`` and return the snapshot id.

    Calls ``table.append()`` once — ONE Iceberg snapshot per batch.

    Returns:
        Snapshot id from ``current_snapshot()``.
    """
    iceberg_table = catalog.load_table(NORMALIZED_ACCESS_FACTS_TABLE)
    iceberg_table.append(pa_table)
    committed = catalog.load_table(NORMALIZED_ACCESS_FACTS_TABLE).current_snapshot()
    return committed.snapshot_id if committed is not None else -1
