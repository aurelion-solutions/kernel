# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DuckDB-backed input loaders for access-analysis detectors.

Only the ``unused`` detector is migrated here (Step 13). Orphan and terminated
detectors remain on their existing SQLAlchemy paths and are not touched.

Cross-engine join policy (Phase 15, Pattern 1):
  - DuckDB reads ``normalized.access_facts`` via ``iceberg_scan``.
  - Per-batch PG aggregate (MAX last_seen) is fetched via AsyncSession + sqlalchemy.text.
  - NEVER ``JOIN postgres_scan('<large_table>')``.
  - NEVER ``fetchall()`` — always ``fetchmany(batch_size)`` to avoid OOM.

Orphan-row policy:
  - If an ``access_usage_facts`` row references an ``access_fact_id`` not present
    in the current Iceberg batch, that usage row is silently dropped. No log emitted
    for this case (would spam during normal lag). Only facts present in Iceberg are
    yielded to the caller.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.policy_assessment.policy_types.access_risk.evaluator import AccessFactView
from src.platform.lake.duckdb_session import LakeSession
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields

_COMPONENT = 'engines.access_analysis'
_TARGET_ID = 'unused_detector_input'


async def _fetch_usage_map(
    pg_session: AsyncSession,
    ids: list[UUID],
) -> dict[UUID, datetime]:
    """Fetch MAX(last_seen) per access_fact_id from PG for a batch of IDs.

    Returns a dict mapping access_fact_id → last_seen datetime.
    Uses Pattern 1 (ANY array) — safe up to pg_any_array_max_size (validated by caller).

    Orphan-row policy: usage rows whose access_fact_id is not in ``ids`` are
    simply not returned (PG WHERE clause excludes them). If an ID in ``ids`` has
    no usage rows, it is absent from the result dict — caller treats it as
    last_seen=None.
    """
    sql = sa.text(
        'SELECT access_fact_id, MAX(last_seen) AS last_seen '
        'FROM access_usage_facts '
        'WHERE access_fact_id = ANY(:ids) '
        'GROUP BY access_fact_id'
    )
    result = await pg_session.execute(sql, {'ids': ids})
    rows = result.all()
    return {row.access_fact_id: row.last_seen for row in rows}


async def iter_unused_access_fact_views(
    lake_session: LakeSession,
    pg_session: AsyncSession,
    log_service: LogService,
    *,
    scope_application_id: UUID | None,
    scope_subject_id: UUID | None,
    batch_size: int = 1000,
    pg_any_array_max_size: int,
) -> AsyncIterator[AccessFactView]:
    """Yield ``AccessFactView`` objects from Iceberg ``normalized.access_facts``.

    Reads via DuckDB ``iceberg_scan`` in deterministic order
    (``application_id_denorm, subject_id, id``). Per batch, fetches usage
    telemetry from PG ``access_usage_facts`` via a single ``ANY($1)`` query.

    Args:
        lake_session: Active DuckDB session with iceberg extension loaded.
        pg_session: Async SQLAlchemy session for PG usage telemetry queries.
        log_service: Injected LogService — all logging goes through here.
        scope_application_id: Optional filter; if set, only facts for this app.
        scope_subject_id: Optional filter; if set, only facts for this subject.
        batch_size: Rows per DuckDB fetchmany call. Must be <= pg_any_array_max_size.
        pg_any_array_max_size: Upper bound for ANY array queries (from LakeSettings).

    Yields:
        ``AccessFactView`` for each active Iceberg row (after orphan-row policy
        and null application_id filtering).

    Raises:
        ValueError: if ``batch_size > pg_any_array_max_size``.
    """
    if batch_size > pg_any_array_max_size:
        raise ValueError(f'batch_size ({batch_size}) exceeds LAKE_PG_ANY_ARRAY_MAX_SIZE ({pg_any_array_max_size})')

    log_service.emit_safe(
        level=LogLevel.INFO,
        message='access_analysis.duckdb_query_started',
        component=_COMPONENT,
        payload=merge_emit_log_participant_fields(
            {
                'query': 'unused_access_facts',
                'scope_application_id': str(scope_application_id) if scope_application_id else None,
                'scope_subject_id': str(scope_subject_id) if scope_subject_id else None,
                'batch_size': batch_size,
            },
            actor_component=_COMPONENT,
            target_id=_TARGET_ID,
        ),
    )

    table_path = lake_session.iceberg_table_path('normalized', 'access_facts')

    # Build parameterised SQL. DuckDB positional params use ?.
    conditions = ['is_active = TRUE']
    params: list[object] = [table_path]

    if scope_application_id is not None:
        conditions.append('application_id_denorm = ?')
        params.append(str(scope_application_id))

    if scope_subject_id is not None:
        conditions.append('subject_id = ?')
        params.append(str(scope_subject_id))

    where_clause = ' AND '.join(conditions)
    sql = (
        f'SELECT id, subject_id, account_id, resource_id, application_id_denorm, valid_from '
        f'FROM iceberg_scan(?) '
        f'WHERE {where_clause} '
        f'ORDER BY application_id_denorm, subject_id, id'
    )

    cursor = await asyncio.to_thread(lake_session.execute, sql, params)

    rows_emitted = 0
    rows_skipped = 0
    batches = 0

    while True:
        batch = await asyncio.to_thread(cursor.fetchmany, batch_size)
        if not batch:
            break

        batches += 1

        # Filter rows with null application_id_denorm or null subject_id.
        # application_id_denorm: schema is required=True but defensive check per spec.
        # subject_id: schema is required=False — orphan-fact rows have no subject and
        # are out of scope for the unused-access detector (covered by orphan_access).
        surviving: list[tuple[object, ...]] = []
        for row in batch:
            row_t = tuple(row)
            if row_t[4] is None:  # application_id_denorm is column index 4
                log_service.emit_safe(
                    level=LogLevel.DEBUG,
                    message='access_analysis.unused_row_skipped_null_application_id',
                    component=_COMPONENT,
                    payload=merge_emit_log_participant_fields(
                        {'row_id': str(row_t[0])},
                        actor_component=_COMPONENT,
                        target_id=_TARGET_ID,
                    ),
                )
                rows_skipped += 1
                continue
            if row_t[1] is None:  # subject_id is column index 1
                log_service.emit_safe(
                    level=LogLevel.DEBUG,
                    message='access_analysis.unused_row_skipped_null_subject_id',
                    component=_COMPONENT,
                    payload=merge_emit_log_participant_fields(
                        {'row_id': str(row_t[0])},
                        actor_component=_COMPONENT,
                        target_id=_TARGET_ID,
                    ),
                )
                rows_skipped += 1
                continue
            surviving.append(row_t)

        if not surviving:
            continue

        # Per-batch PG lookup: MAX(last_seen) for the surviving fact IDs.
        # UUIDs from DuckDB may come back as strings; normalise to UUID objects.
        fact_ids: list[UUID] = [_to_uuid(row[0]) for row in surviving]
        usage_map = await _fetch_usage_map(pg_session, fact_ids)

        for row in surviving:
            fact_id = _to_uuid(row[0])
            subject_id = _to_uuid(row[1])
            account_id = _to_uuid(row[2]) if row[2] is not None else None
            resource_id = _to_uuid(row[3])
            application_id = _to_uuid(row[4])
            valid_from: datetime = row[5]  # type: ignore[assignment]

            # Orphan-row policy: usage rows not in the current batch are simply
            # absent from usage_map — last_seen becomes None. No log for this.
            last_seen: datetime | None = usage_map.get(fact_id)

            yield AccessFactView(
                id=fact_id,
                subject_id=subject_id,
                account_id=account_id,
                resource_id=resource_id,
                application_id=application_id,
                valid_from=valid_from,
                last_seen=last_seen,
            )
            rows_emitted += 1

    log_service.emit_safe(
        level=LogLevel.INFO,
        message='access_analysis.duckdb_query_completed',
        component=_COMPONENT,
        payload=merge_emit_log_participant_fields(
            {
                'query': 'unused_access_facts',
                'rows_emitted': rows_emitted,
                'rows_skipped': rows_skipped,
                'batches': batches,
            },
            actor_component=_COMPONENT,
            target_id=_TARGET_ID,
        ),
    )


def _to_uuid(value: object) -> UUID:
    """Convert a DuckDB UUID column value (may be str or UUID) to ``UUID``."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))
