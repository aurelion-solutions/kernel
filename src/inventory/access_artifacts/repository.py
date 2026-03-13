# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact repository for PostgreSQL access."""

from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_artifacts.models import AccessArtifact


async def create_access_artifact(
    session: AsyncSession,
    *,
    application_id: uuid.UUID,
    source_kind: str,
    external_id: str,
    payload: dict[str, Any],
    ingest_batch_id: str | None = None,
) -> AccessArtifact:
    """Create and persist an access artifact."""
    artifact = AccessArtifact(
        application_id=application_id,
        source_kind=source_kind,
        external_id=external_id,
        payload=payload,
        ingest_batch_id=ingest_batch_id,
    )
    session.add(artifact)
    await session.flush()
    await session.refresh(artifact)
    return artifact


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
    source_kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AccessArtifact]:
    """List access artifacts with optional filters, ordered by ingested_at DESC."""
    query = select(AccessArtifact).order_by(AccessArtifact.ingested_at.desc())
    if application_id is not None:
        query = query.where(AccessArtifact.application_id == application_id)
    if source_kind is not None:
        query = query.where(AccessArtifact.source_kind == source_kind)
    query = query.limit(min(limit, 200)).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())
