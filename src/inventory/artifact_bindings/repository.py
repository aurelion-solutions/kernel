# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ArtifactBinding repository for PostgreSQL access."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.artifact_bindings.models import ArtifactBinding


async def create_artifact_binding(
    session: AsyncSession,
    *,
    artifact_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
) -> ArtifactBinding:
    """Create and persist an artifact binding."""
    binding = ArtifactBinding(
        artifact_id=artifact_id,
        target_type=target_type,
        target_id=target_id,
    )
    session.add(binding)
    await session.flush()
    await session.refresh(binding)
    return binding


async def get_artifact_binding_by_id(
    session: AsyncSession,
    binding_id: uuid.UUID,
) -> ArtifactBinding | None:
    """Load artifact binding by id."""
    result = await session.execute(select(ArtifactBinding).where(ArtifactBinding.id == binding_id))
    return result.scalar_one_or_none()


async def list_artifact_bindings(
    session: AsyncSession,
    *,
    artifact_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ArtifactBinding]:
    """List artifact bindings with optional filters, ordered by created_at DESC."""
    query = select(ArtifactBinding).order_by(ArtifactBinding.created_at.desc())
    if artifact_id is not None:
        query = query.where(ArtifactBinding.artifact_id == artifact_id)
    if target_type is not None:
        query = query.where(ArtifactBinding.target_type == target_type)
    if target_id is not None:
        query = query.where(ArtifactBinding.target_id == target_id)
    query = query.limit(min(limit, 200)).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())
