# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Shared Iceberg append+retire helpers for master data raw tables.

Used by PersonLakeService, OrgUnitLakeService, EmployeeLakeService.
Pattern mirrors AccessArtifactService:
  1. append(new_rows)
  2. scan active rows sharing the same natural key
  3. overwrite(retired_rows, filter=In('id', retiree_ids))
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
import uuid

# ---------------------------------------------------------------------------
# Result type (shared across all master data lake services)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MasterDataBatchResult:
    """Result returned by every master data lake upsert."""

    row_count: int
    snapshot_id: int | None
    backend: Literal['iceberg']


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def ts_micros(value: datetime | None) -> int | None:
    """Convert datetime → microseconds since epoch for PyArrow timestamp arrays."""
    if value is None:
        return None
    ts = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(ts.timestamp() * 1_000_000)


def extract_id(scalar: Any) -> Any:
    return scalar.as_py()


def build_id_filter(ids: list[Any]) -> Any:
    from pyiceberg.expressions import In  # noqa: PLC0415

    return In('id', ids)  # type: ignore[misc, arg-type, call-arg]


def compute_retired_rows(
    scan_arrow: Any,
    *,
    input_keys: set[str],
    key_column: str,
    new_ids: set[Any],
    observed_at: datetime,
) -> Any | None:
    """Return a modified arrow table with superseded rows retired.

    Retires rows where:
    - is_active is True
    - key_column value is in input_keys (key exists in new batch)
    - id is NOT in new_ids (not the freshly appended row)
    """
    import pyarrow as pa  # noqa: PLC0415

    if len(scan_arrow) == 0:
        return None

    id_col = scan_arrow.column('id')
    is_active_col = scan_arrow.column('is_active')
    key_col = scan_arrow.column(key_column)

    retiree_indices = []
    for i in range(len(scan_arrow)):
        if not is_active_col[i].as_py():
            continue
        if extract_id(id_col[i]) in new_ids:
            continue
        if key_col[i].as_py() in input_keys:
            retiree_indices.append(i)

    if not retiree_indices:
        return None

    retirees = scan_arrow.take(retiree_indices)
    tz_us = pa.timestamp('us', tz='UTC')
    ts_val = ts_micros(observed_at)

    new_cols = []
    for name in retirees.column_names:
        if name == 'is_active':
            new_cols.append(pa.array([False] * len(retirees), type=pa.bool_()))
        elif name == 'tombstoned_at':
            new_cols.append(pa.array([ts_val] * len(retirees), type=tz_us))
        else:
            new_cols.append(retirees.column(name))

    return pa.table(
        dict(zip(retirees.column_names, new_cols, strict=True)),
        schema=retirees.schema,
    )


def retire_and_commit(
    table: Any,
    *,
    scan_arrow: Any,
    key_column: str,
    input_keys: set[str],
    new_ids: set[Any],
    observed_at: datetime,
) -> int | None:
    """Retire superseded rows and return the latest snapshot_id (or None)."""
    retired = compute_retired_rows(
        scan_arrow,
        input_keys=input_keys,
        key_column=key_column,
        new_ids=new_ids,
        observed_at=observed_at,
    )
    if retired is None or len(retired) == 0:
        return None

    retiree_ids = [extract_id(retired.column('id')[i]) for i in range(len(retired))]
    table.overwrite(retired, overwrite_filter=build_id_filter(retiree_ids))

    try:
        return table.metadata.current_snapshot_id  # type: ignore[no-any-return]
    except Exception:
        return None


def scan_active_by_keys(table: Any, *, key_column: str, keys: list[str]) -> Any:
    """Scan for active rows matching any of the given key values."""
    from pyiceberg.expressions import And, EqualTo, In  # noqa: PLC0415

    row_filter: Any = And(
        EqualTo('is_active', True),  # type: ignore[misc, arg-type, call-arg]
        In(key_column, keys),  # type: ignore[misc, arg-type, call-arg]
    )
    return table.scan(row_filter=row_filter).to_arrow()


def latest_snapshot_id(table: Any) -> int | None:
    try:
        return table.metadata.current_snapshot_id  # type: ignore[no-any-return]
    except Exception:
        return None


def new_row_id() -> str:
    return str(uuid.uuid4())


def now_micros(now: datetime) -> int:
    aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return int(aware.timestamp() * 1_000_000)
