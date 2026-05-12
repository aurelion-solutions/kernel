# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Internal DTOs for the Effective Access Store — Step 3 batch projection driver.

This module contains only internal, DB-facing transfer objects used between the
repository and service layers.  HTTP-facing schemas (request/response models for
the read API and rebuild endpoint) ship with Step 4.

Models:
- ``AccessFactRow`` — denormalized fact row returned by the repository.  Contains
  ``subject_kind`` (resolved via JOIN subjects) and ``application_id`` (resolved
  via JOIN resources), which are NOT columns on the ``access_facts`` table — the
  repository owns this resolution so the service and projector stay pure.
- ``InitiativeRow`` — row from the ``initiatives`` table.
- ``ProjectionScopeKind`` — three-value StrEnum discriminating per-fact,
  per-application, and per-initiative scope.
- ``ProjectionRunSummary`` — summary returned by each service call and used to
  build the ``eas.projection.completed`` event payload.  Uses Pydantic BaseModel
  (not dataclass) because this DTO *will* cross the HTTP boundary in Step 4 —
  choosing Pydantic now avoids a conversion layer then.

All models use ``ConfigDict(frozen=True, extra='forbid', strict=True)`` for
consistency with the Step 2 projector DTOs and to catch type drift early in
tests.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator
from src.engines.effective_access.models import EffectiveGrantEffect
from src.inventory.access_facts.schemas import AccessFactEffect
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind

_CFG = ConfigDict(frozen=True, extra='forbid', strict=True)


class ProjectionScopeKind(StrEnum):
    """Discriminates the three projection scope modes."""

    ACCESS_FACT = 'access_fact'
    APPLICATION = 'application'
    INITIATIVE = 'initiative'


class IncrementalApplyKind(StrEnum):
    """Discriminates the three incremental-apply branches."""

    UPSERT = 'upsert'
    INVALIDATE_FACT = 'invalidate_fact'
    INVALIDATE_INITIATIVE = 'invalidate_initiative'


class AccessFactRow(BaseModel):
    """Denormalized access-fact row as returned by the repository layer.

    ``subject_kind`` and ``application_id`` are resolved by the repository
    via JOIN — they are not columns on ``access_facts``.
    """

    model_config = _CFG

    id: UUID
    subject_id: UUID
    subject_kind: SubjectKind
    account_id: UUID | None
    application_id: UUID
    resource_id: UUID
    action: Action
    effect: AccessFactEffect
    valid_from: datetime
    valid_until: datetime | None


class InitiativeRow(BaseModel):
    """Initiative row as returned by the repository layer."""

    model_config = _CFG

    id: UUID
    access_fact_id: UUID
    type: InitiativeType
    origin: str
    valid_from: datetime
    valid_until: datetime | None


# ---------------------------------------------------------------------------
# Step 4 — HTTP-facing response schemas
# ---------------------------------------------------------------------------


class EffectiveGrantRead(BaseModel):
    """HTTP response schema for a single EffectiveGrant row.

    Uses ``from_attributes=True`` so ``model_validate(orm_row)`` works directly.
    16 fields match every column on ``EffectiveGrant`` except none.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    subject_id: UUID
    subject_kind: SubjectKind
    application_id: UUID
    account_id: UUID | None
    resource_id: UUID
    action: Action
    effect: EffectiveGrantEffect
    initiative_type: InitiativeType
    initiative_origin: str
    valid_from: datetime
    valid_until: datetime | None
    source_access_fact_id: UUID
    source_initiative_id: UUID
    observed_at: datetime
    tombstoned_at: datetime | None


class EffectiveGrantExplainResult(BaseModel):
    """Internal service DTO returned by ``EffectiveAccessReadService.explain_access``.

    Parallel to ``EffectiveGrantExplainResponse`` — same shape, separate class to
    keep the internal↔external boundary explicit (mirrors the Step 3 pattern of
    keeping ``ProjectionRunSummary`` internal).
    """

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    effect: Literal['allow', 'deny', 'none']
    grants: list[EffectiveGrantRead]


class EffectiveGrantExplainResponse(BaseModel):
    """HTTP response schema for ``GET /effective-grants/explain``.

    ``effect`` is the deny-wins aggregation of the current projection state —
    NOT a policy decision. PDP (Phase 06) is authoritative for allow/deny verdicts;
    this endpoint reports raw projection rows only.

    ``effect='none'`` when no non-tombstoned matching rows exist.
    ``effect='allow'`` when all active matches carry ``effect=allow``.
    ``effect='deny'`` when any active match carries ``effect=deny`` (deny-wins).
    """

    model_config = ConfigDict(from_attributes=True)

    effect: Literal['allow', 'deny', 'none']
    grants: list[EffectiveGrantRead]


class ProjectionRunSummary(BaseModel):
    """Summary of one completed projection scope call.

    Returned by ``EffectiveAccessProjectionService.project_access_fact`` and
    ``project_application``.  Also used as the payload source for the
    ``eas.projection.completed`` event.

    Field semantics:
    - ``rows_upserted`` — total rows touched (inserted + updated).
    - ``rows_inserted`` — rows where ``xmax = 0`` (new rows).
    - ``rows_updated`` — ``rows_upserted - rows_inserted``.
    - ``rows_tombstoned`` — rows where ``tombstoned_at IS NOT NULL`` after upsert.
    - ``pairs_projected`` — total ``(fact, initiative)`` pairs fed to ``project()``.
    """

    model_config = _CFG

    scope_kind: ProjectionScopeKind
    scope_id: UUID
    pairs_projected: int
    rows_upserted: int
    rows_inserted: int
    rows_updated: int
    rows_tombstoned: int
    rows_skipped: int = 0
    started_at: datetime
    finished_at: datetime
    correlation_id: UUID


# ---------------------------------------------------------------------------
# Phase 18 Step 9b — Action envelope schemas (projection write surface)
# ---------------------------------------------------------------------------


class ProjectAccessFactArgs(BaseModel):
    """Args for effective_access.project_access_fact action.

    ``now`` controls the projection timestamp passed to ``project()``.
    ``correlation_id`` is optional — the service generates one when absent.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    access_fact_id: UUID
    now: datetime
    correlation_id: UUID | None = None


class ProjectApplicationArgs(BaseModel):
    """Args for effective_access.project_application action."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    application_id: UUID
    now: datetime
    correlation_id: UUID | None = None


class ApplyIncrementalChangeArgs(BaseModel):
    """Args for effective_access.apply_incremental_change action.

    Mirrors the service's branching contract:
    - UPSERT / INVALIDATE_FACT require ``access_fact_id``; ``initiative_id`` must be absent.
    - INVALIDATE_INITIATIVE requires ``initiative_id``; ``access_fact_id`` must be absent.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    change_kind: IncrementalApplyKind
    observed_at: datetime
    access_fact_id: UUID | None = None
    initiative_id: UUID | None = None
    correlation_id: UUID | None = None
    causation_event_id: UUID | None = None

    @model_validator(mode='after')
    def _validate_branch_ids(self) -> ApplyIncrementalChangeArgs:
        """Enforce XOR: access_fact_id vs initiative_id per change_kind."""
        if self.change_kind is IncrementalApplyKind.INVALIDATE_INITIATIVE:
            if self.initiative_id is None:
                raise ValueError('INVALIDATE_INITIATIVE requires initiative_id')
            if self.access_fact_id is not None:
                raise ValueError('INVALIDATE_INITIATIVE must not receive access_fact_id')
        else:
            if self.access_fact_id is None:
                raise ValueError(f'{self.change_kind.value} requires access_fact_id')
            if self.initiative_id is not None:
                raise ValueError(f'{self.change_kind.value} must not receive initiative_id')
        return self


class ProjectionResult(BaseModel):
    """Envelope wrapping a ``ProjectionRunSummary`` for action result validation.

    Non-frozen because it wraps derived counters and may be constructed from
    a service return value with ``model_validate(...)``.
    """

    model_config = ConfigDict(extra='forbid')

    scope_kind: str
    scope_id: UUID
    pairs_projected: int
    rows_upserted: int
    rows_inserted: int
    rows_updated: int
    rows_tombstoned: int
    rows_skipped: int
    started_at: datetime
    finished_at: datetime
    correlation_id: UUID
