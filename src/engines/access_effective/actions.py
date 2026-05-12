# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine actions for the effective_access slice — read and projection surfaces.

Read actions (idempotent=True):
  - (effective_access, list_grants)              → EffectiveAccessReadService.list_grants
  - (effective_access, explain_access)           → EffectiveAccessReadService.explain_access
  - (effective_access, get_grant)                → EffectiveAccessReadService.get_grant

Projection actions (idempotent=False):
  - (effective_access, project_access_fact)      → EffectiveAccessProjectionService.project_access_fact
  - (effective_access, project_application)      → EffectiveAccessProjectionService.project_application
  - (effective_access, apply_incremental_change) → EffectiveAccessProjectionService.apply_incremental_change

Library-module discipline: no get_settings(), no load_dotenv(),
no register_default_providers() at import time.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.engines.access_effective.models import EffectiveGrantEffect
from src.engines.access_effective.schemas import (
    ApplyIncrementalChangeArgs,
    EffectiveGrantExplainResponse,
    EffectiveGrantRead,
    ProjectAccessFactArgs,
    ProjectApplicationArgs,
    ProjectionResult,
    ProjectionRunSummary,
)
from src.engines.access_effective.service import (
    EffectiveAccessProjectionService,
    EffectiveAccessReadService,
)
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind
from src.platform.orchestrator.registry import ActionContext, register_action

# ---------------------------------------------------------------------------
# Args schemas
# ---------------------------------------------------------------------------


class ListGrantsArgs(BaseModel):
    """Args for effective_access.list_grants action.

    Mirrors the 13 query parameters of GET /effective-grants with identical
    types, defaults, and bounds.  Mandatory-filter rule: at least one of
    subject_id, resource_id, application_id, source_initiative_id must be set.
    """

    model_config = ConfigDict(extra='forbid')

    subject_id: UUID | None = None
    subject_kind: SubjectKind | None = None
    application_id: UUID | None = None
    account_id: UUID | None = None
    resource_id: UUID | None = None
    action: Action | None = None
    effect: EffectiveGrantEffect | None = None
    initiative_type: InitiativeType | None = None
    initiative_origin: str | None = Field(default=None, max_length=1024)
    source_initiative_id: UUID | None = None
    active_only: bool = True
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def _require_at_least_one_filter(self) -> ListGrantsArgs:
        """Enforce mandatory-filter rule — mirrors the HTTP 400 guard in routes.py."""
        if (
            self.subject_id is None
            and self.resource_id is None
            and self.application_id is None
            and self.source_initiative_id is None
        ):
            raise ValueError(
                'at least one of subject_id, resource_id, application_id, source_initiative_id is required'
            )
        return self


class ExplainAccessArgs(BaseModel):
    """Args for effective_access.explain_access action."""

    model_config = ConfigDict(extra='forbid')

    subject_id: UUID
    resource_id: UUID
    action: Action
    active_only: bool = True


class GetGrantArgs(BaseModel):
    """Args for effective_access.get_grant action."""

    model_config = ConfigDict(extra='forbid')

    grant_id: UUID


# ---------------------------------------------------------------------------
# Result schemas
# ---------------------------------------------------------------------------


class ListGrantsResult(BaseModel):
    """Envelope wrapping the list_grants result.

    Actions must return a BaseModel; a bare list[...] is not accepted by the
    registry's result_schema validation.  The envelope also echoes pagination
    parameters for caller clarity.
    """

    model_config = ConfigDict(extra='forbid')

    grants: list[EffectiveGrantRead]
    limit: int
    offset: int


class GetGrantResult(BaseModel):
    """Envelope for get_grant result.

    grant=None represents "row not found" — this is a successful read with
    empty payload, not an error.  The HTTP route raises 404; the action
    contract uses Optional to signal absence without failing the step run.
    """

    model_config = ConfigDict(extra='forbid')

    grant: EffectiveGrantRead | None


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


@register_action(  # type: ignore[arg-type]
    engine='access_effective',
    action='list_grants',
    args_schema=ListGrantsArgs,
    result_schema=ListGrantsResult,
    idempotent=True,
)
async def list_grants_action(args: ListGrantsArgs, ctx: ActionContext) -> ListGrantsResult:
    """Return a page of effective grants matching the given filters."""
    service = EffectiveAccessReadService(ctx.session)
    rows = await service.list_grants(
        subject_id=args.subject_id,
        subject_kind=args.subject_kind,
        application_id=args.application_id,
        account_id=args.account_id,
        resource_id=args.resource_id,
        action=args.action,
        effect=args.effect,
        initiative_type=args.initiative_type,
        initiative_origin=args.initiative_origin,
        source_initiative_id=args.source_initiative_id,
        active_only=args.active_only,
        limit=args.limit,
        offset=args.offset,
    )
    grants = [EffectiveGrantRead.model_validate(r) for r in rows]
    return ListGrantsResult(grants=grants, limit=args.limit, offset=args.offset)


@register_action(  # type: ignore[arg-type]
    engine='access_effective',
    action='explain_access',
    args_schema=ExplainAccessArgs,
    result_schema=EffectiveGrantExplainResponse,
    idempotent=True,
)
async def explain_access_action(args: ExplainAccessArgs, ctx: ActionContext) -> EffectiveGrantExplainResponse:
    """Return deny-wins aggregation for the given (subject, resource, action) triple."""
    service = EffectiveAccessReadService(ctx.session)
    result = await service.explain_access(
        subject_id=args.subject_id,
        resource_id=args.resource_id,
        action=args.action,
        active_only=args.active_only,
    )
    return EffectiveGrantExplainResponse.model_validate(result, from_attributes=True)


@register_action(  # type: ignore[arg-type]
    engine='access_effective',
    action='get_grant',
    args_schema=GetGrantArgs,
    result_schema=GetGrantResult,
    idempotent=True,
)
async def get_grant_action(args: GetGrantArgs, ctx: ActionContext) -> GetGrantResult:
    """Fetch a single effective grant by id; grant=None when not found."""
    service = EffectiveAccessReadService(ctx.session)
    row = await service.get_grant(args.grant_id)
    grant = EffectiveGrantRead.model_validate(row) if row is not None else None
    return GetGrantResult(grant=grant)


# ---------------------------------------------------------------------------
# Projection action handlers (idempotent=False — they mutate effective_grants)
# ---------------------------------------------------------------------------


def _summary_to_result(summary: ProjectionRunSummary) -> ProjectionResult:
    """Convert a ProjectionRunSummary to a ProjectionResult envelope."""
    return ProjectionResult(
        scope_kind=summary.scope_kind.value,
        scope_id=summary.scope_id,
        pairs_projected=summary.pairs_projected,
        rows_upserted=summary.rows_upserted,
        rows_inserted=summary.rows_inserted,
        rows_updated=summary.rows_updated,
        rows_tombstoned=summary.rows_tombstoned,
        rows_skipped=summary.rows_skipped,
        started_at=summary.started_at,
        finished_at=summary.finished_at,
        correlation_id=summary.correlation_id,
    )


@register_action(  # type: ignore[arg-type]
    engine='access_effective',
    action='project_access_fact',
    args_schema=ProjectAccessFactArgs,
    result_schema=ProjectionResult,
    idempotent=False,
)
async def project_access_fact_action(args: ProjectAccessFactArgs, ctx: ActionContext) -> ProjectionResult:
    """Project all (fact, initiative) pairs for one AccessFact and upsert them."""
    service = EffectiveAccessProjectionService(ctx.session)
    summary = await service.project_access_fact(
        access_fact_id=args.access_fact_id,
        now=args.now,
        correlation_id=args.correlation_id,
    )
    return _summary_to_result(summary)


@register_action(  # type: ignore[arg-type]
    engine='access_effective',
    action='project_application',
    args_schema=ProjectApplicationArgs,
    result_schema=ProjectionResult,
    idempotent=True,
)
async def project_application_action(args: ProjectApplicationArgs, ctx: ActionContext) -> ProjectionResult:
    """Project all (fact, initiative) pairs for one Application and upsert them."""
    service = EffectiveAccessProjectionService(ctx.session)
    summary = await service.project_application(
        application_id=args.application_id,
        now=args.now,
        correlation_id=args.correlation_id,
    )
    return _summary_to_result(summary)


@register_action(  # type: ignore[arg-type]
    engine='access_effective',
    action='apply_incremental_change',
    args_schema=ApplyIncrementalChangeArgs,
    result_schema=ProjectionResult,
    idempotent=False,
)
async def apply_incremental_change_action(args: ApplyIncrementalChangeArgs, ctx: ActionContext) -> ProjectionResult:
    """Apply one incremental change event to the effective_grants projection."""
    service = EffectiveAccessProjectionService(ctx.session)
    summary = await service.apply_incremental_change(
        change_kind=args.change_kind,
        observed_at=args.observed_at,
        access_fact_id=args.access_fact_id,
        initiative_id=args.initiative_id,
        correlation_id=args.correlation_id,
        causation_event_id=args.causation_event_id,
    )
    return _summary_to_result(summary)
