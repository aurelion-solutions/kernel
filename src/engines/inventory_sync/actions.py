# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine action for the sync_apply slice.

One action registered at import time via @register_action:

  - (sync_apply, apply) → SyncApplyService.apply

idempotent=True:
  - Run-level: ``SyncApplyAlreadyExecutedError`` raised when an active run
    already exists for the same ``reconciliation_run_id`` — retry fails fast.
  - Item-level: ``preflight_recover_already_written`` (first statement in
    ``_apply_batch``) scans Iceberg for items already written but not yet
    PG-flagged. Covers the crash-between-commit window.

Library-module discipline: no get_settings(), no load_dotenv(),
no register_default_providers() at import time. Service construction reads
os.environ only inside the handler body via _build_event_service().
"""

from __future__ import annotations

import os
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator
from src.engines.inventory_sync.deps import _make_simple_denorm_resolver
from src.engines.inventory_sync.models import SyncApplyRunMode, SyncApplyRunStatus
from src.engines.inventory_sync.service import SyncApplyService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService
from src.platform.lake.factory import (
    get_process_lake_catalog,
    get_process_lake_session,
)
from src.platform.orchestrator.registry import ActionContext, register_action

# ---------------------------------------------------------------------------
# Args schema
# ---------------------------------------------------------------------------


class SyncApplyApplyArgs(BaseModel):
    """Args for sync_apply.apply action."""

    model_config = ConfigDict(extra='forbid')

    reconciliation_run_id: UUID
    mode: SyncApplyRunMode
    item_ids: list[UUID] | None = None
    requested_by: str | None = None
    correlation_id: str | None = None

    @model_validator(mode='after')
    def _validate_item_ids(self) -> SyncApplyApplyArgs:
        if self.mode == SyncApplyRunMode.selected_items:
            if not self.item_ids:
                raise ValueError('item_ids must be non-empty when mode=selected_items')
        else:
            if self.item_ids:
                raise ValueError('item_ids must be None or empty unless mode=selected_items')
        return self


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------


class SyncApplyApplyResult(BaseModel):
    """Result envelope for sync_apply.apply action.

    Shaped from SyncApplyApplyResponse via model_validate(from_attributes=True).
    Not a subclass — avoids pulling in Field constraints from the response schema.
    """

    model_config = ConfigDict(extra='forbid')

    apply_run_id: UUID
    status: SyncApplyRunStatus
    applied_count: int
    failed_count: int
    snapshot_ids: dict[str, int]


# ---------------------------------------------------------------------------
# Service construction helpers
# ---------------------------------------------------------------------------


def _build_event_service() -> EventService:
    """Build EventService from AURELION_EVENTS_PROVIDER env var (same as deps.py)."""
    provider = os.environ.get('AURELION_EVENTS_PROVIDER', 'noop')
    sink = event_sink_factory.get(provider)
    return EventService(sink=sink)


async def _build_sync_apply_service(ctx: ActionContext) -> SyncApplyService:
    """Construct SyncApplyService with process-scoped lake deps.

    lake_session and catalog are obtained from the process-level factory
    (set_process_lake_deps) rather than FastAPI request state.
    EventService is built from AURELION_EVENTS_PROVIDER env var.
    LogService comes from ctx.log_service.

    Note: SyncApplyService does not accept lake_settings — not passed here.
    """
    lake_session = await get_process_lake_session()
    catalog = get_process_lake_catalog()
    events = _build_event_service()
    denorm_resolver = _make_simple_denorm_resolver()
    return SyncApplyService(
        session=ctx.session,
        lake_session=lake_session,
        catalog=catalog,
        denorm_resolver=denorm_resolver,
        events=events,
        logs=ctx.log_service,
    )


# ---------------------------------------------------------------------------
# Action handler
# ---------------------------------------------------------------------------


@register_action(  # type: ignore[arg-type]
    engine='inventory_sync',
    action='apply',
    args_schema=SyncApplyApplyArgs,
    result_schema=SyncApplyApplyResult,
    idempotent=True,
)
async def apply_action(args: SyncApplyApplyArgs, ctx: ActionContext) -> SyncApplyApplyResult:
    """Delegate to SyncApplyService.apply and reshape response into result envelope."""
    service = await _build_sync_apply_service(ctx)
    response = await service.apply(
        reconciliation_run_id=args.reconciliation_run_id,
        mode=args.mode,
        item_ids=args.item_ids,
        requested_by=args.requested_by,
        correlation_id=args.correlation_id,
    )
    return SyncApplyApplyResult.model_validate(response, from_attributes=True)
