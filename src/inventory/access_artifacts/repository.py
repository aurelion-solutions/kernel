# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact repository for PostgreSQL access.

Table `access_artifacts` is a plain (non-partitioned) table.
The `xmax = 0` RETURNING idiom used in `upsert_access_artifact` is therefore
safe — PostgreSQL 17/18 forbid system columns in RETURNING only for partitioned
targets. See ARCH_CONTEXT.md section "System columns in RETURNING on partitioned
tables" for the full carve-out. If `access_artifacts` is ever partitioned,
this function must be rewritten to use the pre-SELECT set-difference strategy
described in that section.

Concurrent-writer caveat: under concurrent writers hitting the same identity
triple, `was_inserted` may overcount inserts vs updates (race-insert window).
The resulting row state is always exact. For Phase 12 Step 8 the reconciliation
orchestrator is serialized per `application_id`, so this is a forward note only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func
from src.inventory.access_artifacts.models import AccessArtifact


async def upsert_access_artifact(
    session: AsyncSession,
    *,
    application_id: uuid.UUID,
    artifact_type: str,
    external_id: str,
    payload: dict[str, Any],
    ingest_batch_id: str | None,
    observed_at: datetime,
    raw_name: str | None = None,
    effect: str | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> tuple[AccessArtifact, bool]:
    """Upsert an access artifact on the identity triple (application_id, artifact_type, external_id).

    On conflict with constraint ``uq_access_artifacts_application_id_artifact_type_external_id``,
    refreshes ``payload``, ``observed_at``, ``ingest_batch_id``, ``ingested_at``, and the four
    permitted universal fields (``raw_name``, ``effect``, ``valid_from``, ``valid_until``) in place.
    All four are observation-content fields — same refresh semantics as ``payload``.
    Passing ``None`` for any of the four explicitly sets them to ``NULL`` (current-state semantics).

    Reactivation semantics (Step 11): ``is_active`` is reset to ``True`` and ``tombstoned_at``
    to ``NULL`` on every upsert UPDATE. A source resurfacing a previously-tombstoned
    ``external_id`` revives the same row in place — "re-grant = reactivate, never insert a new
    row" (phase_12.md). No ``.reactivated`` event is emitted for this transition; see Q3 in
    TASK.md for the deferred-observability rationale.

    Returns:
        (artifact, was_inserted) — ``was_inserted`` is True on a fresh INSERT,
        False on an UPDATE of an existing row. Derived from ``RETURNING (xmax = 0)``.
    """
    insert_stmt = insert(AccessArtifact).values(
        application_id=application_id,
        artifact_type=artifact_type,
        external_id=external_id,
        payload=payload,
        ingest_batch_id=ingest_batch_id,
        observed_at=observed_at,
        raw_name=raw_name,
        effect=effect,
        valid_from=valid_from,
        valid_until=valid_until,
    )
    stmt = insert_stmt.on_conflict_do_update(
        constraint='uq_access_artifacts_application_id_artifact_type_external_id',
        set_={
            'payload': insert_stmt.excluded.payload,
            'observed_at': insert_stmt.excluded.observed_at,
            'ingest_batch_id': insert_stmt.excluded.ingest_batch_id,
            'ingested_at': func.now(),
            'raw_name': insert_stmt.excluded.raw_name,
            'effect': insert_stmt.excluded.effect,
            'valid_from': insert_stmt.excluded.valid_from,
            'valid_until': insert_stmt.excluded.valid_until,
            # Reactivation: revive tombstoned rows on re-observation.
            'is_active': True,
            'tombstoned_at': None,
        },
    ).returning(
        AccessArtifact,
        sa.literal_column('(xmax = 0)').label('was_inserted'),
    )

    result = await session.execute(stmt)
    row = result.one()
    artifact: AccessArtifact = row[0]
    was_inserted: bool = bool(row[1])
    return artifact, was_inserted


async def get_access_artifact_by_id(
    session: AsyncSession,
    artifact_id: uuid.UUID,
) -> AccessArtifact | None:
    """Load access artifact by id."""
    result = await session.execute(select(AccessArtifact).where(AccessArtifact.id == artifact_id))
    return result.scalar_one_or_none()


async def list_access_artifacts(
    session: AsyncSession,
    *,
    application_id: uuid.UUID | None = None,
    artifact_type: str | None = None,
    is_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AccessArtifact]:
    """List access artifacts with optional filters, ordered by ingested_at DESC."""
    query = select(AccessArtifact).order_by(AccessArtifact.ingested_at.desc())
    if application_id is not None:
        query = query.where(AccessArtifact.application_id == application_id)
    if artifact_type is not None:
        query = query.where(AccessArtifact.artifact_type == artifact_type)
    if is_active is not None:
        query = query.where(AccessArtifact.is_active == is_active)
    query = query.limit(min(limit, 200)).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def tombstone_access_artifact(
    session: AsyncSession,
    *,
    artifact_id: uuid.UUID,
    observed_at: datetime,
) -> tuple[AccessArtifact | None, bool]:
    """Tombstone an access artifact by id.

    Executes ``UPDATE ... WHERE id=:artifact_id AND is_active=true RETURNING *``.

    Returns:
        (artifact, True)  — row was active and has been tombstoned.
        (artifact, False) — row exists but was already inactive (idempotent no-op).
        (None, False)     — row does not exist.
    """
    stmt = (
        sa.update(AccessArtifact)
        .where(
            AccessArtifact.id == artifact_id,
            AccessArtifact.is_active.is_(True),
        )
        .values(is_active=False, tombstoned_at=observed_at, observed_at=observed_at)
        .returning(AccessArtifact)
        .execution_options(synchronize_session='fetch')
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is not None:
        return row, True

    # Zero rows updated — either already tombstoned or not found.
    existing = await session.execute(select(AccessArtifact).where(AccessArtifact.id == artifact_id))
    artifact = existing.scalar_one_or_none()
    if artifact is not None:
        return artifact, False
    return None, False
