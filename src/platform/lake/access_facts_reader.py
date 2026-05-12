# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Physical DuckDB read path for ``normalized.access_facts``.

Public entry points:
  - :func:`run_get_fact` — single-row fetch by fact id.
  - :func:`run_list_facts` — LIMIT/OFFSET paginated scan with filters.
  - :func:`run_get_delta_item_id` — lake scan for reconciliation_delta_item_id.
  - :func:`run_get_artifact_from_iceberg` — projection (application_id, external_id)
    from ``raw.access_artifacts`` used in the artifact-ref drill-down.
  - :func:`run_get_by_natural_key` — active fact lookup by natural key.

Row shape:
  - Returns :class:`AccessFactRow` typed dataclass (lake-level row shape, not domain DTO).
  - :data:`SCAN_COLUMNS` is the public column-order contract — the façade uses it
    to build the ``AccessFactRow`` → ``AccessFactView`` mapping without
    re-declaring the column list.

No cursor pagination — ``normalized.access_facts`` uses LIMIT/OFFSET only.
No error classes — readers return ``None`` for not-found.

Library module — MUST NOT call load_dotenv() / get_settings() at import time.
Forbidden imports: src.inventory.*, src.engines.*, src.products.*,
                   pyiceberg, pyarrow (DuckDB only via lake_session.execute).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid

# ---------------------------------------------------------------------------
# Lake-level row shape (NOT a domain DTO — no Pydantic, no inventory imports)
# ---------------------------------------------------------------------------

#: Ordered column names for the DuckDB iceberg_scan SELECT.
#: This is the public column-order contract — the façade maps AccessFactRow → AccessFactView.
SCAN_COLUMNS: tuple[str, ...] = (
    'id',
    'subject_id',
    'account_id',
    'resource_id',
    'action_id',
    'action_slug',
    'effect',
    'valid_from',
    'valid_until',
    'is_active',
    'revoked_at',
    'observed_at',
    'created_at',
)


@dataclass(frozen=True, slots=True)
class AccessFactRow:
    """Lake-level row returned by the reader.

    Field names and order match :data:`SCAN_COLUMNS`.
    This is a typed boundary sentinel — NOT a domain DTO (no Pydantic, not named
    after a domain concept). The façade maps this to ``AccessFactView``.
    """

    id: Any
    subject_id: Any
    account_id: Any
    resource_id: Any
    action_id: Any
    action_slug: Any
    effect: Any
    valid_from: Any
    valid_until: Any
    is_active: Any
    revoked_at: Any
    observed_at: Any
    created_at: Any


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _row_tuple_to_row(row: tuple[Any, ...]) -> AccessFactRow:
    """Convert a DuckDB fetchall tuple to a typed :class:`AccessFactRow`."""
    return AccessFactRow(**dict(zip(SCAN_COLUMNS, row, strict=True)))


# ---------------------------------------------------------------------------
# Public entry points (sync — caller wraps in asyncio.to_thread)
# ---------------------------------------------------------------------------


def run_get_fact(
    lake_session: Any,
    *,
    warehouse_uri: str,
    fact_id: uuid.UUID,
) -> AccessFactRow | None:
    """Fetch single access fact by id via DuckDB iceberg_scan.

    Sync — must be called via ``asyncio.to_thread`` from async context.

    Returns:
        :class:`AccessFactRow` or ``None`` if not found.
    """
    sql = f"""
        SELECT
            f.id, f.subject_id, f.account_id, f.resource_id,
            f.action_id, r.slug AS action_slug, f.effect,
            f.valid_from, f.valid_until, f.is_active, f.revoked_at,
            f.observed_at, f.created_at
        FROM iceberg_scan('{warehouse_uri}/normalized/access_facts') f
        LEFT JOIN ref_actions_local r ON r.id = CAST(f.action_id AS BIGINT)
        WHERE f.id = ?::uuid AND f.id IS NOT NULL
        LIMIT 1
    """
    lake_session.execute(sql, [str(fact_id)])
    rows = lake_session.fetchmany(1)
    if not rows:
        return None
    return _row_tuple_to_row(rows[0])


def run_list_facts(
    lake_session: Any,
    *,
    warehouse_uri: str,
    subject_id: uuid.UUID | None,
    resource_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
    action_slug: str | None,
    effect_value: str | None,
    is_active: bool | None,
    valid_at: datetime | None,
    limit: int,
    offset: int,
) -> list[AccessFactRow]:
    """Execute DuckDB iceberg_scan for access facts with filters.

    Sync — must be called via ``asyncio.to_thread`` from async context.

    ``effect_value`` is the raw string value of the effect enum (e.g. ``"allow"``).
    The façade performs ``.value`` extraction before calling this function.

    Returns:
        List of :class:`AccessFactRow` matching the given filters.
    """
    predicates: list[str] = ['f.id IS NOT NULL']
    params: list[Any] = []

    if subject_id is not None:
        predicates.append('f.subject_id = ?::uuid')
        params.append(str(subject_id))
    if resource_id is not None:
        predicates.append('f.resource_id = ?::uuid')
        params.append(str(resource_id))
    if account_id is not None:
        predicates.append('f.account_id = ?::uuid')
        params.append(str(account_id))
    if action_slug is not None:
        predicates.append('r.slug = ?')
        params.append(action_slug)
    if effect_value is not None:
        predicates.append('f.effect = ?')
        params.append(effect_value)
    if is_active is not None:
        predicates.append('f.is_active = ?')
        params.append(is_active)
    if valid_at is not None:
        predicates.append('f.valid_from <= ?')
        params.append(valid_at)
        predicates.append('(f.valid_until IS NULL OR f.valid_until >= ?)')
        params.append(valid_at)

    where_clause = 'WHERE ' + ' AND '.join(predicates)

    sql = f"""
        SELECT
            f.id, f.subject_id, f.account_id, f.resource_id,
            f.action_id, r.slug AS action_slug, f.effect,
            f.valid_from, f.valid_until, f.is_active, f.revoked_at,
            f.observed_at, f.created_at
        FROM iceberg_scan('{warehouse_uri}/normalized/access_facts') f
        LEFT JOIN ref_actions_local r ON r.id = CAST(f.action_id AS BIGINT)
        {where_clause}
        ORDER BY f.id
        LIMIT ?
        OFFSET ?
    """
    params.append(min(limit, 200))
    params.append(offset)

    lake_session.execute(sql, params)
    rows: list[AccessFactRow] = []
    while True:
        batch = lake_session.fetchmany(500)
        if not batch:
            break
        for row in batch:
            rows.append(_row_tuple_to_row(row))
    return rows


def run_get_delta_item_id(
    lake_session: Any,
    *,
    warehouse_uri: str,
    fact_id: uuid.UUID,
) -> uuid.UUID | None:
    """DuckDB iceberg_scan for reconciliation_delta_item_id by fact id.

    Sync — must be called via ``asyncio.to_thread`` from async context.

    Returns:
        The ``reconciliation_delta_item_id`` UUID or ``None`` if not found.
    """
    path = f'{warehouse_uri}/normalized/access_facts'
    sql = """
        SELECT reconciliation_delta_item_id
        FROM iceberg_scan(?)
        WHERE id = ?::varchar AND id IS NOT NULL
        LIMIT 1
    """
    lake_session.execute(sql, [path, str(fact_id)])
    rows = lake_session.fetchall()
    if not rows:
        return None
    raw_val = rows[0][0]
    if raw_val is None:
        return None
    try:
        return uuid.UUID(str(raw_val))
    except ValueError:
        return None


def run_get_artifact_from_iceberg(
    lake_session: Any,
    *,
    warehouse_uri: str,
    artifact_id: uuid.UUID,
) -> tuple[uuid.UUID, str] | None:
    """DuckDB iceberg_scan for (application_id, external_id) from raw.access_artifacts.

    Sync — must be called via ``asyncio.to_thread`` from async context.

    Used in the artifact-ref drill-down step 3: source_artifact_id → fields.

    Returns:
        ``(application_id, external_id)`` tuple or ``None`` if not found.
    """
    path = f'{warehouse_uri}/raw/access_artifacts'
    sql = """
        SELECT application_id, external_id
        FROM iceberg_scan(?)
        WHERE id = ?::varchar AND id IS NOT NULL
        LIMIT 1
    """
    lake_session.execute(sql, [path, str(artifact_id)])
    rows = lake_session.fetchall()
    if not rows:
        return None
    app_id_raw, ext_id = rows[0][0], rows[0][1]
    if app_id_raw is None or ext_id is None:
        return None
    try:
        app_id = uuid.UUID(str(app_id_raw))
    except ValueError:
        return None
    return app_id, str(ext_id)


def run_get_by_natural_key(
    lake_session: Any,
    *,
    warehouse_uri: str,
    subject_id: uuid.UUID,
    account_id: uuid.UUID | None,
    resource_id: uuid.UUID,
    action_slug: str,
) -> AccessFactRow | None:
    """DuckDB iceberg_scan for active access fact by natural key.

    Sync — must be called via ``asyncio.to_thread`` from async context.

    Returns:
        :class:`AccessFactRow` or ``None`` if not found.
    """
    predicates = [
        'f.subject_id = ?::uuid',
        'f.resource_id = ?::uuid',
        'r.slug = ?',
        'f.is_active = true',
        'f.id IS NOT NULL',
    ]
    params: list[Any] = [str(subject_id), str(resource_id), action_slug]

    if account_id is None:
        predicates.append('f.account_id IS NULL')
    else:
        predicates.append('f.account_id = ?::uuid')
        params.append(str(account_id))

    where_clause = 'WHERE ' + ' AND '.join(predicates)

    sql = f"""
        SELECT
            f.id, f.subject_id, f.account_id, f.resource_id,
            f.action_id, r.slug AS action_slug, f.effect,
            f.valid_from, f.valid_until, f.is_active, f.revoked_at,
            f.observed_at, f.created_at
        FROM iceberg_scan('{warehouse_uri}/normalized/access_facts') f
        LEFT JOIN ref_actions_local r ON r.id = CAST(f.action_id AS BIGINT)
        {where_clause}
        LIMIT 1
    """

    lake_session.execute(sql, params)
    rows = lake_session.fetchmany(1)
    if not rows:
        return None
    return _row_tuple_to_row(rows[0])
