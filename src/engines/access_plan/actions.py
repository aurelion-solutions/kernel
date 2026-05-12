# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine actions for the access_plan slice (Phase 19 Steps E1/E3/E5).

Registered actions:
  - (access_plan, plan) — wrapper around AccessPlanService.create_plan
    for orchestrator-driven planning invocations.
  - (access_plan, fanout_replan_for_initiative) — resolves the subject_ref from
    an initiative record (or a hint in args) and calls create_plan once.
  - (access_plan, fanout_replan_for_application) — queries all NHIs for an
    application and calls create_plan for each one (N plans per application).
  - (access_plan, cleanup_stale_apply_leases) — stateless scanner that removes
    stale rows from access_apply_active.

Action logic for fanout_replan_for_initiative:
  1. If args.subject_ref is already set → call create_plan directly (fast path).
  2. Otherwise → lookup Initiative by initiative_id → use access_fact_id to
     resolve subject. If the initiative is not found, log and return gracefully.

Action logic for fanout_replan_for_application:
  1. Query nhis WHERE application_id = args.application_id.
  2. For each NHI call create_plan(subject_ref=nhi.id,
     idempotency_key=f"{application_id}:{nhi_id}").
  3. Return the count of plans created/reused.

Action logic for cleanup_stale_apply_leases:
  For each row in access_apply_active:
    1. Fetch pipeline_run status via platform.orchestrator.repository.
    2. If status is terminal (completed | failed | failed_timeout | cancelled)
       → DELETE the row (the worker is done / died and its finally block did not clean up).
    3. If run row not found → DELETE (orphaned lease, pipeline_run was purged).
    4. If started_at < now() - max_apply_duration_seconds → log WARN + DELETE
       (defensive: protects against a stuck/hung run that never transitions to terminal).

idempotent=True: scanning and deleting stale rows is safe to run multiple times.

Library-module discipline: no get_settings(), no load_dotenv(),
no register_default_providers() at import time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_plan.models import AccessApplyActive
from src.inventory.nhi.repository import list_nhi_by_application_id
from src.platform.logs.schemas import LogLevel
from src.platform.orchestrator.registry import ActionContext, register_action
from src.platform.orchestrator.repository import get_pipeline_run_status, is_terminal
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

_COMPONENT = 'engines.access_plan'


# ---------------------------------------------------------------------------
# access_plan.plan action
# ---------------------------------------------------------------------------


class PlanArgs(BaseModel):
    """Args for access_plan.plan action.

    Wraps AccessPlanService.create_plan for orchestrator-driven planning.
    """

    model_config = ConfigDict(extra='forbid')

    subject_ref: str
    idempotency_key: str | None = None
    context_overrides: dict[str, Any] | None = None


class PlanResult(BaseModel):
    """Result envelope for access_plan.plan action."""

    model_config = ConfigDict(extra='forbid')

    plan_id: str
    subject_ref: str
    subject_type: str
    status: str
    requires_confirmation: bool
    content_hash: str
    supersedes_plan_id: str | None = None


@register_action(  # type: ignore[arg-type]
    engine='access_plan',
    action='plan',
    args_schema=PlanArgs,
    result_schema=PlanResult,
    idempotent=True,
)
async def plan_action(args: PlanArgs, ctx: ActionContext) -> PlanResult:
    """Action handler: create (or reuse) an AccessPlan via service.create_plan."""
    from src.engines.access_plan.service import AccessPlanService
    from src.engines.policy_assessment.generative.service import GenerativePDPService

    pdp = GenerativePDPService()
    settings = RuntimeSettingsConfig()
    service = AccessPlanService(
        session=ctx.session,
        pdp_service=pdp,
        settings=settings,
    )

    plan = await service.create_plan(
        subject_ref=args.subject_ref,
        idempotency_key=args.idempotency_key,
        context_overrides=args.context_overrides,
    )

    return PlanResult(
        plan_id=str(plan.id),
        subject_ref=plan.subject_ref,
        subject_type=plan.subject_type,
        status=plan.status.value,
        requires_confirmation=plan.requires_confirmation,
        content_hash=plan.content_hash,
        supersedes_plan_id=str(plan.supersedes_plan_id) if plan.supersedes_plan_id else None,
    )


# ---------------------------------------------------------------------------
# access_plan.fanout_replan_for_initiative action
# ---------------------------------------------------------------------------


class FanoutReplanForInitiativeArgs(BaseModel):
    """Args for access_plan.fanout_replan_for_initiative action.

    Either ``subject_ref`` is provided directly from the event payload (fast path),
    or the action looks up the Initiative by ``initiative_id`` to resolve the subject.
    """

    model_config = ConfigDict(extra='forbid')

    initiative_id: str
    subject_ref: str | None = None
    idempotency_key: str | None = None


class FanoutReplanForInitiativeResult(BaseModel):
    """Result envelope for access_plan.fanout_replan_for_initiative action."""

    model_config = ConfigDict(extra='forbid')

    plan_id: str | None = None
    subject_ref: str | None = None
    skipped: bool = False
    skip_reason: str | None = None


@register_action(  # type: ignore[arg-type]
    engine='access_plan',
    action='fanout_replan_for_initiative',
    args_schema=FanoutReplanForInitiativeArgs,
    result_schema=FanoutReplanForInitiativeResult,
    idempotent=True,
)
async def fanout_replan_for_initiative_action(
    args: FanoutReplanForInitiativeArgs,
    ctx: ActionContext,
) -> FanoutReplanForInitiativeResult:
    """Action handler: resolve subject from initiative and create a plan.

    Fast path: args.subject_ref is already set — call create_plan directly.
    Slow path: lookup Initiative by initiative_id → access_fact_id → skip if
    subject cannot be resolved (graceful degradation).
    """
    from src.engines.access_plan.service import AccessPlanService
    from src.engines.policy_assessment.generative.service import GenerativePDPService
    from src.inventory.initiatives.repository import get_initiative_by_id

    subject_ref = args.subject_ref

    if subject_ref is None:
        # Resolve from initiative record: Initiative.access_fact_id does not
        # carry subject_ref directly (access_facts live in Iceberg lake). The
        # caller must have populated subject_ref in the event payload.
        # If it is absent, log and skip gracefully.
        try:
            initiative_id = uuid.UUID(args.initiative_id)
        except ValueError:
            return FanoutReplanForInitiativeResult(
                skipped=True,
                skip_reason=f'invalid initiative_id format: {args.initiative_id!r}',
            )

        initiative = await get_initiative_by_id(ctx.session, initiative_id)
        if initiative is None:
            ctx.log_service.emit_safe(
                level=LogLevel.WARNING,
                message='fanout_replan_for_initiative: initiative not found',
                component=_COMPONENT,
                payload={
                    'initiative_id': args.initiative_id,
                    'skip_reason': 'initiative_not_found',
                },
            )  # allowed-emit-safe: observability
            return FanoutReplanForInitiativeResult(
                skipped=True,
                skip_reason='initiative_not_found',
            )

        ctx.log_service.emit_safe(
            level=LogLevel.DEBUG,
            message='fanout_replan_for_initiative: no subject_ref in payload, skipping',
            component=_COMPONENT,
            payload={
                'initiative_id': args.initiative_id,
                'access_fact_id': str(initiative.access_fact_id),
                'skip_reason': 'subject_ref_missing_from_payload',
            },
        )  # allowed-emit-safe: observability
        return FanoutReplanForInitiativeResult(
            skipped=True,
            skip_reason='subject_ref_missing_from_payload',
        )

    pdp = GenerativePDPService()
    settings = RuntimeSettingsConfig()
    service = AccessPlanService(
        session=ctx.session,
        pdp_service=pdp,
        settings=settings,
    )

    plan = await service.create_plan(
        subject_ref=subject_ref,
        idempotency_key=args.idempotency_key,
    )

    return FanoutReplanForInitiativeResult(
        plan_id=str(plan.id),
        subject_ref=subject_ref,
        skipped=False,
    )


# ---------------------------------------------------------------------------
# access_plan.fanout_replan_for_application action
# ---------------------------------------------------------------------------


class FanoutReplanForApplicationArgs(BaseModel):
    """Args for access_plan.fanout_replan_for_application action.

    Receives application_id from the inventory.application.decommissioned payload.
    Queries all NHIs for that application and triggers one plan per NHI.
    """

    model_config = ConfigDict(extra='forbid')

    application_id: str
    idempotency_key: str | None = None


class FanoutReplanForApplicationResult(BaseModel):
    """Result envelope for access_plan.fanout_replan_for_application action."""

    model_config = ConfigDict(extra='forbid')

    application_id: str
    nhi_count: int
    plans_created: int


@register_action(  # type: ignore[arg-type]
    engine='access_plan',
    action='fanout_replan_for_application',
    args_schema=FanoutReplanForApplicationArgs,
    result_schema=FanoutReplanForApplicationResult,
    idempotent=True,
)
async def fanout_replan_for_application_action(
    args: FanoutReplanForApplicationArgs,
    ctx: ActionContext,
) -> FanoutReplanForApplicationResult:
    """Action handler: create one plan for each NHI belonging to the application.

    Idempotency: each per-NHI plan uses idempotency_key=f"{application_id}:{nhi_id}"
    so re-delivery of the same application.decommissioned event is collapsed.
    """
    from src.engines.access_plan.service import AccessPlanService
    from src.engines.policy_assessment.generative.service import GenerativePDPService

    try:
        application_id = uuid.UUID(args.application_id)
    except ValueError:
        ctx.log_service.emit_safe(
            level=LogLevel.ERROR,
            message='fanout_replan_for_application: invalid application_id',
            component=_COMPONENT,
            payload={'application_id': args.application_id},
        )  # allowed-emit-safe: observability
        return FanoutReplanForApplicationResult(
            application_id=args.application_id,
            nhi_count=0,
            plans_created=0,
        )

    nhis = await list_nhi_by_application_id(ctx.session, application_id)

    if not nhis:
        ctx.log_service.emit_safe(
            level=LogLevel.INFO,
            message='fanout_replan_for_application: no NHIs found for application',
            component=_COMPONENT,
            payload={'application_id': args.application_id},
        )  # allowed-emit-safe: observability
        return FanoutReplanForApplicationResult(
            application_id=args.application_id,
            nhi_count=0,
            plans_created=0,
        )

    pdp = GenerativePDPService()
    settings = RuntimeSettingsConfig()
    service = AccessPlanService(
        session=ctx.session,
        pdp_service=pdp,
        settings=settings,
    )

    from src.engines.access_plan.repository import resolve_subject_ref_for_nhi  # noqa: PLC0415

    plans_created = 0
    for nhi in nhis:
        nhi_idempotency_key = f'{args.application_id}:{nhi.id}'
        subject_ref = await resolve_subject_ref_for_nhi(ctx.session, nhi.id)
        if subject_ref is None:
            ctx.log_service.emit_safe(
                level=LogLevel.WARNING,
                message='fanout_replan_for_application: no Subject row for NHI, skipping',
                component=_COMPONENT,
                payload={'nhi_id': str(nhi.id), 'application_id': args.application_id},
            )  # allowed-emit-safe: observability
            continue
        await service.create_plan(
            subject_ref=subject_ref,
            idempotency_key=nhi_idempotency_key,
        )
        plans_created += 1

    return FanoutReplanForApplicationResult(
        application_id=args.application_id,
        nhi_count=len(nhis),
        plans_created=plans_created,
    )


# ---------------------------------------------------------------------------
# Args / Result schemas (cleanup_stale_apply_leases)
# ---------------------------------------------------------------------------


class CleanupStaleApplyLeasesArgs(BaseModel):
    """Args for access_plan.cleanup_stale_apply_leases action (no args required)."""

    model_config = ConfigDict(extra='forbid')


class CleanupStaleApplyLeasesResult(BaseModel):
    """Result envelope for access_plan.cleanup_stale_apply_leases action."""

    model_config = ConfigDict(extra='forbid')

    rows_inspected: int
    rows_deleted: int


# ---------------------------------------------------------------------------
# Core cleanup logic (extracted for testability)
# ---------------------------------------------------------------------------


async def cleanup_stale_apply_leases(
    session: AsyncSession,
    settings: RuntimeSettingsConfig | None = None,
    now: datetime | None = None,
) -> CleanupStaleApplyLeasesResult:
    """Scan access_apply_active and delete stale lease rows.

    Called by the registered action handler and directly in tests.
    Session discipline: flushes implicit inserts; caller commits.
    """
    effective_settings = settings if settings is not None else RuntimeSettingsConfig()
    effective_now = now if now is not None else datetime.now(UTC)

    stale_threshold = effective_now - timedelta(seconds=effective_settings.max_apply_duration_seconds)

    # Fetch all active lease rows
    result = await session.execute(sa.select(AccessApplyActive))
    rows: list[AccessApplyActive] = list(result.scalars().all())

    rows_deleted = 0

    for row in rows:
        run_id = row.pipeline_run_id
        subject_ref = row.subject_ref

        # Check pipeline run status
        status = await get_pipeline_run_status(session, run_id)

        if status is None:
            # Pipeline run row not found — orphaned lease, delete it
            await session.execute(sa.delete(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref))
            rows_deleted += 1
            continue

        if is_terminal(status):
            # Worker finished (successfully or with error) but did not clean up lease
            await session.execute(sa.delete(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref))
            rows_deleted += 1
            continue

        # Defensive timeout check: lease is older than max_apply_duration
        started_at = row.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)

        if started_at < stale_threshold:
            # Log warning and delete
            if hasattr(session, 'info') and 'log_service' in (session.info or {}):
                log_svc = session.info['log_service']  # type: ignore[index]
                await log_svc.emit_log(  # allowed-emit-safe: best-effort warning
                    level=LogLevel.WARNING,
                    component=_COMPONENT,
                    message=(
                        f'Stale apply lease deleted: subject_ref={subject_ref!r} '
                        f'pipeline_run_id={run_id} started_at={started_at.isoformat()} '
                        f'status={status.value!r} max_apply_duration_seconds='
                        f'{effective_settings.max_apply_duration_seconds}'
                    ),
                )
            await session.execute(sa.delete(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref))
            rows_deleted += 1

    return CleanupStaleApplyLeasesResult(
        rows_inspected=len(rows),
        rows_deleted=rows_deleted,
    )


# ---------------------------------------------------------------------------
# Registered action handler
# ---------------------------------------------------------------------------


@register_action(  # type: ignore[arg-type]
    engine='access_plan',
    action='cleanup_stale_apply_leases',
    args_schema=CleanupStaleApplyLeasesArgs,
    result_schema=CleanupStaleApplyLeasesResult,
    idempotent=True,
)
async def cleanup_stale_apply_leases_action(
    args: CleanupStaleApplyLeasesArgs,  # noqa: ARG001
    ctx: ActionContext,
) -> CleanupStaleApplyLeasesResult:
    """Action handler: scan and delete stale access_apply_active rows."""
    settings = RuntimeSettingsConfig()
    return await cleanup_stale_apply_leases(ctx.session, settings=settings)


def _ensure_plan_registered() -> None:
    """Re-register access_plan.plan if cleared by _clear_for_tests.  Test-only."""
    from src.platform.orchestrator.registry import ACTION_REGISTRY  # noqa: PLC0415

    ACTION_REGISTRY._register_if_absent(  # type: ignore[attr-defined]
        engine='access_plan',
        action='plan',
        args_schema=PlanArgs,
        result_schema=PlanResult,
        idempotent=True,
        handler=plan_action,  # type: ignore[arg-type]
    )
