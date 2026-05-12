# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine actions for the reconciliation slice.

Two actions registered at import time via @register_action:

  - (reconciliation, run)               → ReconciliationService.run
  - (reconciliation, master_data_apply) → apply_master_data_delta

Both are idempotent=True:
  - ``run``: advisory lock on application_id prevents concurrent duplicate runs.
  - ``master_data_apply``: status guard — ``apply_master_data_delta`` rejects
    runs not in ``pending_apply`` status; a successful first call moves status
    forward so a retry deterministically fails fast.

Library-module discipline: no get_settings(), no load_dotenv(),
no register_default_providers() at import time. Service construction reads
os.environ only inside the handler body via _build_event_service().
"""

from __future__ import annotations

from datetime import datetime
import os
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator
from src.engines.reconciliation.master_data_apply import apply_master_data_delta
from src.engines.reconciliation.models import ReconciliationEntityType
from src.engines.reconciliation.schemas import ReconciliationRunMode, ReconciliationRunSummary
from src.engines.reconciliation.service import ReconciliationService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService
from src.platform.lake.factory import (
    get_process_lake_catalog,
    get_process_lake_session,
    get_process_lake_settings,
)
from src.platform.orchestrator.registry import ActionContext, register_action

# ---------------------------------------------------------------------------
# Args schemas
# ---------------------------------------------------------------------------


class ReconciliationRunArgs(BaseModel):
    """Args for reconciliation.run action."""

    model_config = ConfigDict(extra='forbid')

    application_id: UUID
    mode: ReconciliationRunMode = ReconciliationRunMode.review
    correlation_id: str | None = None


class MasterDataApplyArgs(BaseModel):
    """Args for reconciliation.master_data_apply action."""

    model_config = ConfigDict(extra='forbid')

    run_id: UUID
    entity_type: ReconciliationEntityType
    correlation_id: str | None = None

    @model_validator(mode='after')
    def _reject_access_fact(self) -> MasterDataApplyArgs:
        """Reject access_fact entity type at the schema boundary."""
        if self.entity_type == ReconciliationEntityType.access_fact:
            raise ValueError(
                'entity_type=access_fact is not valid for master_data_apply; '
                'use sync_apply.apply for access fact application'
            )
        return self


# ---------------------------------------------------------------------------
# Result schemas
# ---------------------------------------------------------------------------


class ReconciliationRunResult(BaseModel):
    """Result envelope for reconciliation.run action.

    Shaped from ReconciliationRunSummary via model_validate(from_attributes=True).
    Not a subclass — avoids pulling in Field constraints from the summary schema.
    """

    model_config = ConfigDict(extra='forbid')

    run_id: UUID | None
    application_id: UUID
    started_at: datetime
    finished_at: datetime
    facts_created: int
    facts_updated: int
    facts_revoked: int
    unchanged_count: int
    observed_snapshot_id: int | None
    current_snapshot_id: int | None


class MasterDataApplyActionResult(BaseModel):
    """Result envelope for reconciliation.master_data_apply action.

    Distinct from the dataclass MasterDataApplyResult in master_data_apply.py —
    uses Pydantic BaseModel as required by the registry result_schema contract.
    """

    model_config = ConfigDict(extra='forbid')

    run_id: UUID
    entity_type: ReconciliationEntityType
    applied_count: int
    failed_count: int
    ignored_count: int


# ---------------------------------------------------------------------------
# Service construction helper
# ---------------------------------------------------------------------------


def _build_event_service() -> EventService:
    """Build EventService from AURELION_EVENTS_PROVIDER env var (same as deps.py)."""
    provider = os.environ.get('AURELION_EVENTS_PROVIDER', 'noop')
    sink = event_sink_factory.get(provider)
    return EventService(sink=sink)


async def _build_reconciliation_service(ctx: ActionContext) -> ReconciliationService:
    """Construct ReconciliationService with process-scoped lake deps.

    lake_session, catalog, and lake_settings are obtained from the process-level
    factory (set_process_lake_deps) rather than FastAPI request state.
    EventService is built from AURELION_EVENTS_PROVIDER env var.
    LogService comes from ctx.log_service.
    """
    lake_session = await get_process_lake_session()
    catalog = get_process_lake_catalog()
    lake_settings = get_process_lake_settings()
    events = _build_event_service()
    return ReconciliationService(
        session=ctx.session,
        lake_session=lake_session,
        catalog=catalog,
        events=events,
        logs=ctx.log_service,
        lake_settings=lake_settings,
    )


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


@register_action(  # type: ignore[arg-type]
    engine='reconciliation',
    action='run',
    args_schema=ReconciliationRunArgs,
    result_schema=ReconciliationRunResult,
    idempotent=True,
)
async def run_action(args: ReconciliationRunArgs, ctx: ActionContext) -> ReconciliationRunResult:
    """Delegate to ReconciliationService.run and reshape summary into result envelope."""
    service = await _build_reconciliation_service(ctx)
    summary: ReconciliationRunSummary = await service.run(
        args.application_id,
        mode=args.mode,
        correlation_id=args.correlation_id,
    )
    return ReconciliationRunResult.model_validate(summary, from_attributes=True)


@register_action(  # type: ignore[arg-type]
    engine='reconciliation',
    action='master_data_apply',
    args_schema=MasterDataApplyArgs,
    result_schema=MasterDataApplyActionResult,
    idempotent=True,
)
async def master_data_apply_action(args: MasterDataApplyArgs, ctx: ActionContext) -> MasterDataApplyActionResult:
    """Delegate to apply_master_data_delta and reshape result into action envelope."""
    result = await apply_master_data_delta(
        ctx.session,
        run_id=args.run_id,
        entity_type=args.entity_type,
    )
    return MasterDataApplyActionResult(
        run_id=result.run_id,
        entity_type=result.entity_type,
        applied_count=result.applied_count,
        failed_count=result.failed_count,
        ignored_count=result.ignored_count,
    )
