# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine actions for the access_apply slice (renamed from provisioning in Phase 19 Step A4).

Three actions registered at import time via @register_action:

  - (access_apply, create_account) → engines.access_apply.create_account.create_account
  - (access_apply, delete_account) → engines.access_apply.delete_account.delete_account
  - (access_apply, execute_plan)   → engines.access_apply.execute_plan.execute_plan (F1+F3)

idempotent=True contract rationale:
  The action declares ``idempotent=True`` on the orchestrator-runner contract.
  execute_plan is idempotent: items in 'done' status are skipped on restart;
  preflight verify_fact catches the case where the connector succeeded but the
  kernel crashed before writing 'done'.  F3 chain operations are also idempotent
  (sync_single_fact event_key check; create_or_get / close no-ops on repeat).

Library-module discipline: no ``get_settings()``, no ``load_dotenv()``,
no ``register_default_providers()`` at import time.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from src.engines.access_apply.create_account import create_account
from src.engines.access_apply.delete_account import delete_account
from src.engines.access_apply.execute_plan import ExecutePlanCounts, execute_plan
from src.engines.access_apply.schemas import AccountCreateRequest
from src.engines.inventory_sync.deps import _make_simple_denorm_resolver
from src.engines.inventory_sync.service import SyncApplyService
from src.inventory.initiatives.service import InitiativeService
from src.platform.connectors.factory import get_process_connector_client
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService
from src.platform.lake.factory import get_process_lake_catalog, get_process_lake_session
from src.platform.orchestrator.registry import ActionContext, register_action

if TYPE_CHECKING:
    from src.platform.connectors.client import ConnectorClient

# ---------------------------------------------------------------------------
# Args / Result schemas
# ---------------------------------------------------------------------------


class CreateAccountArgs(BaseModel):
    """Args for access_apply.create_account action."""

    model_config = ConfigDict(extra='forbid')

    application_id: UUID
    username: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)


class CreateAccountResult(BaseModel):
    """Result envelope for access_apply.create_account action."""

    model_config = ConfigDict(extra='forbid')

    username: str
    email: str | None
    status: str


class DeleteAccountArgs(BaseModel):
    """Args for access_apply.delete_account action."""

    model_config = ConfigDict(extra='forbid')

    application_id: UUID
    username: str = Field(min_length=1, max_length=255)


class DeleteAccountResult(BaseModel):
    """Result envelope for access_apply.delete_account action."""

    model_config = ConfigDict(extra='forbid')

    status: str


# ---------------------------------------------------------------------------
# Service construction helpers
# ---------------------------------------------------------------------------


def _build_connector_client() -> ConnectorClient:
    """Wrap get_process_connector_client() for stable patch target in tests."""
    return get_process_connector_client()


def _build_event_service() -> EventService:
    """Build EventService from AURELION_EVENTS_PROVIDER env var."""
    provider = os.environ.get('AURELION_EVENTS_PROVIDER', 'noop')
    sink = event_sink_factory.get(provider)
    return EventService(sink=sink)


async def _build_sync_service(ctx: ActionContext) -> SyncApplyService:
    """Construct SyncApplyService with process-scoped lake deps for F3 chain."""
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


def _build_initiative_service() -> InitiativeService:
    """Build InitiativeService with noop event service for F3 chain.

    Events from initiative create_or_get/close are emitted internally by
    InitiativeService; noop is sufficient here because the caller's
    EventService (via access_apply) does not need to re-emit them.
    """
    events = _build_event_service()
    return InitiativeService(event_service=events)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


@register_action(  # type: ignore[arg-type]
    engine='access_apply',
    action='create_account',
    args_schema=CreateAccountArgs,
    result_schema=CreateAccountResult,
    idempotent=True,
)
async def create_account_action(args: CreateAccountArgs, ctx: ActionContext) -> CreateAccountResult:
    """Delegate to access_apply.create_account and return a result envelope."""
    connector = _build_connector_client()
    result_dict = await create_account(
        ctx.session,
        args.application_id,
        AccountCreateRequest(username=args.username, email=args.email),
        connector,
        log_service=ctx.log_service,
    )
    return CreateAccountResult.model_validate(result_dict)


@register_action(  # type: ignore[arg-type]
    engine='access_apply',
    action='delete_account',
    args_schema=DeleteAccountArgs,
    result_schema=DeleteAccountResult,
    idempotent=True,
)
async def delete_account_action(args: DeleteAccountArgs, ctx: ActionContext) -> DeleteAccountResult:
    """Delegate to access_apply.delete_account and synthesise accepted status."""
    connector = _build_connector_client()
    await delete_account(
        ctx.session,
        args.application_id,
        args.username,
        connector,
        log_service=ctx.log_service,
    )
    return DeleteAccountResult(status='accepted')


# ---------------------------------------------------------------------------
# execute_plan — stub (full implementation in Phase 19 Step F1)
# ---------------------------------------------------------------------------


class ExecutePlanArgs(BaseModel):
    """Args for access_apply.execute_plan action."""

    model_config = ConfigDict(extra='forbid')

    plan_id: UUID


class ExecutePlanResult(BaseModel):
    """Result envelope for access_apply.execute_plan action."""

    model_config = ConfigDict(extra='forbid')

    plan_id: str
    items_attempted: int
    items_done: int
    items_failed: int


@register_action(  # type: ignore[arg-type]
    engine='access_apply',
    action='execute_plan',
    args_schema=ExecutePlanArgs,
    result_schema=ExecutePlanResult,
    idempotent=True,
)
async def execute_plan_action(args: ExecutePlanArgs, ctx: ActionContext) -> ExecutePlanResult:
    """Apply all items of an AccessPlan (Phase 19 Steps F1+F3).

    Iterates PlanItems in topological DAG order:
    - Skips items already in 'done' status (restart idempotency).
    - Preflight verify_fact: if match → run F3 chain (idempotent), mark done.
    - Sets status to 'executing', calls connector, post-verify.
    - On post-verify success: run F3 chain (lake write + Initiative), auto-invalidate
      other active plans, mark item done.
    - On post-verify failure: marks item failed with failure_reason.
    - Finally: releases subject-level apply lease from access_apply_active.

    F3 chain: lake write (sync_single_fact) is always first; PG writes (Initiative
    create_or_get / close) happen in the same per-item commit as status=done.

    Commits after each item — items are independent persisted work units.
    """
    connector = _build_connector_client()
    sync_service = await _build_sync_service(ctx)
    initiative_service = _build_initiative_service()
    counts: ExecutePlanCounts = await execute_plan(
        ctx.session,
        args.plan_id,
        ctx.pipeline_run_id,
        connector,
        log_service=ctx.log_service,
        sync_service=sync_service,
        initiative_service=initiative_service,
    )
    return ExecutePlanResult(
        plan_id=str(counts.plan_id),
        items_attempted=counts.items_attempted,
        items_done=counts.items_done,
        items_failed=counts.items_failed,
    )


def _ensure_execute_plan_registered() -> None:
    """Re-register access_apply.execute_plan if cleared by _clear_for_tests.  Test-only."""
    from src.platform.orchestrator.registry import ACTION_REGISTRY  # noqa: PLC0415

    ACTION_REGISTRY._register_if_absent(  # type: ignore[attr-defined]
        engine='access_apply',
        action='execute_plan',
        args_schema=ExecutePlanArgs,
        result_schema=ExecutePlanResult,
        idempotent=True,
        handler=execute_plan_action,  # type: ignore[arg-type]
    )
