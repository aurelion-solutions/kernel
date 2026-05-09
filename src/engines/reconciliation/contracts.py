# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reconciliation handler contracts: NormalizationResult frozen dataclass + Handler Protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_artifacts.schemas import AccessArtifactView


@dataclass(frozen=True)
class NormalizationResult:
    """One normalized access fact candidate produced by a handler.

    Invariants:
    - Exactly one of ``subject_id`` or ``account_id`` must be set.
      Handlers MUST set at least one; the engine rejects results where both
      are None.
    - ``resource_id`` is already resolved by the handler via
      ``ensure_resource_by_identity(...)``; handlers MUST NOT return raw
      ``resource_key`` / ``resource_type``.
    - ``action_slug`` must be a slug from the seeded ``ref_actions`` vocabulary.
      Unknown slug → 422 at service level.
    - ``effect`` is the raw source string (``allow`` | ``deny``); validated
      downstream by ``AccessFact.effect`` enum.
    """

    subject_id: UUID | None
    account_id: UUID | None
    resource_id: UUID
    action_slug: str
    effect: str
    valid_from: datetime | None
    valid_until: datetime | None


class Handler(Protocol):
    """Structural contract for artifact-type handlers.

    Handlers are stateless.  They may read from the DB (e.g. resolve
    ``Resource`` via ``ensure_resource_by_identity``).  They MUST NOT commit,
    flush ``AccessFact``, or emit events — those concerns belong to the engine.

    Phase 15 Step 16: artifact parameter retyped from AccessArtifact ORM to
    AccessArtifactView (frozen Pydantic v2 DTO).
    """

    async def handle(
        self,
        artifact: AccessArtifactView,
        session: AsyncSession,
    ) -> list[NormalizationResult]: ...


class HandlerAlreadyRegisteredError(Exception):
    """Raised by ``register_handler`` when ``artifact_type`` is already registered."""
