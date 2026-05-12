# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""REST endpoints for the access_plan engine (Phase 19 Step E1).

Endpoints:
  POST   /plans                — create (or reuse) an AccessPlan
  POST   /plans/dry-run        — compute plan without persisting
  GET    /plans                — list plans with filters + pagination
  GET    /plans/{id}           — get single plan by id
  POST   /plans/{id}/apply     — create pipeline run to apply the plan

Route declaration order matters:
  /dry-run  MUST be declared before  /{id}  to avoid UUID-parse ambiguity.

Error shapes follow existing kernel pattern:
  {"detail": "<message>", "code": "<code>"}

Authorization: existing kernel auth middleware (Phase 1 JWT/secret manager).
No new scopes added in Phase 19.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.engines.access_plan.deps import (
    get_access_plan_service,
    get_plan_orchestrator_service,
    get_plan_pipelines,
)
from src.engines.access_plan.display_enrichment import enrich_plan_items
from src.engines.access_plan.models import (
    AccessApplyActive,
    AccessPlan,
    AccessPlanStatus,
    PlanDependency,
    PlanItem,
    PlanItemExecution,
    PlanItemExecutionStatus,
    PlanItemKind,
)
from src.engines.access_plan.repository import (
    count_plan_items_cross_plan,
    list_plan_items_cross_plan,
)
from src.engines.access_plan.schemas import PlanItemCountResponse, PlanItemListResponse, PlanItemRead
from src.engines.access_plan.service import (
    AccessPlanService,
    SubjectNotFoundError,
)
from src.platform.orchestrator.models import PipelineTriggerSource
from src.platform.orchestrator.repository import get_pipeline_run_status, is_terminal
from src.platform.orchestrator.service import PipelineOrchestratorService

router = APIRouter(prefix='/plans', tags=['access-plans'])

_DependsDB = Depends(get_db)
DependsService = Depends(get_access_plan_service)
DependsOrchestratorService = Depends(get_plan_orchestrator_service)
DependsPipelines = Depends(get_plan_pipelines)

# -----------------------------------------------------------------------
# Pipeline name for access_apply
# -----------------------------------------------------------------------

_ACCESS_APPLY_PIPELINE_NAME = 'access_apply_pipeline'


# -----------------------------------------------------------------------
# Response schemas (inline Pydantic-free — use plain dicts per existing pattern)
# We use typed response_model dicts via Any for brevity; the shapes are
# documented in the docstrings and enforced by tests.
# -----------------------------------------------------------------------


def _plan_to_dict(plan: AccessPlan, items: list[PlanItem] | None = None) -> dict[str, Any]:
    """Serialize AccessPlan + optional items to response dict."""
    d: dict[str, Any] = {
        'id': str(plan.id),
        'subject_ref': plan.subject_ref,
        'subject_type': plan.subject_type,
        'status': plan.status.value,
        'requires_confirmation': plan.requires_confirmation,
        'content_hash': plan.content_hash,
        'created_at': plan.created_at.isoformat() if plan.created_at else None,
        'supersedes_plan_id': str(plan.supersedes_plan_id) if plan.supersedes_plan_id else None,
        'invalidation_reason': plan.invalidation_reason.value if plan.invalidation_reason else None,
        'invalidated_by_plan_id': str(plan.invalidated_by_plan_id) if plan.invalidated_by_plan_id else None,
        'idempotency_key': plan.idempotency_key,
    }
    if items is not None:
        d['items'] = [_item_to_dict(i) for i in items]
    return d


def _item_to_dict(item: PlanItem) -> dict[str, Any]:
    return {
        'id': str(item.id),
        'plan_id': str(item.plan_id),
        'kind': item.kind.value,
        'application': item.application,
        'account_ref': item.account_ref,
        'target_descriptor': item.target_descriptor,
        'initiatives': item.initiatives,
        'initiative_refs': item.initiative_refs,
        'policy_rule_refs': item.policy_rule_refs,
        'decision_snapshot': item.decision_snapshot,
    }


def _execution_to_dict(execution: PlanItemExecution) -> dict[str, Any]:
    return {
        'plan_id': str(execution.plan_id),
        'item_id': str(execution.item_id),
        'status': execution.status.value,
        'failure_reason': execution.failure_reason.value if execution.failure_reason else None,
        'last_verified_at': execution.last_verified_at.isoformat() if execution.last_verified_at else None,
        'last_error': execution.last_error,
    }


# -----------------------------------------------------------------------
# POST /plans
# -----------------------------------------------------------------------


@router.post('', status_code=201)
async def create_plan(
    body: dict[str, Any] = Body(...),
    service: AccessPlanService = DependsService,
    session: AsyncSession = _DependsDB,
) -> dict[str, Any]:
    """Create (or reuse) an AccessPlan.

    Body:
      subject_ref: str          — UUID string of the subject
      idempotency_key?: str     — optional idempotency key
      context_overrides?: dict  — optional what-if overrides

    Returns 201 + plan content (or 200 + reused plan on idempotency_key / hash hit).
    Returns 404 when subject_ref does not exist.
    """
    subject_ref: str | None = body.get('subject_ref')
    if not subject_ref:
        raise HTTPException(status_code=422, detail='subject_ref is required')

    idempotency_key: str | None = body.get('idempotency_key')
    context_overrides: dict[str, Any] | None = body.get('context_overrides')

    with translate_service_errors({SubjectNotFoundError: (404, lambda e: f'Subject not found: {e.subject_ref}')}):
        plan = await service.create_plan(
            subject_ref=subject_ref,
            idempotency_key=idempotency_key,
            context_overrides=context_overrides,
        )

    # Fetch items for response
    items_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == plan.id))
    items = list(items_result.scalars().all())

    await session.commit()

    # Determine if this was a reuse (idempotency hit → 200)
    # We check if the plan was just created by comparing created_at recency.
    # Simpler: return 201 unless we detect a reused plan is being returned for idempotency.
    # For now, all creates return 201 (reuse cases handled in the service transparently).
    response_dict = _plan_to_dict(plan, items)
    return response_dict


# -----------------------------------------------------------------------
# POST /plans/dry-run  (MUST be before /{id})
# -----------------------------------------------------------------------


@router.post('/dry-run', status_code=200)
async def create_plan_dry_run(
    body: dict[str, Any] = Body(...),
    service: AccessPlanService = DependsService,
    session: AsyncSession = _DependsDB,
) -> dict[str, Any]:
    """Compute plan without persisting to DB.

    Same logic as POST /plans but the plan (and items) are NOT committed.
    Useful for what-if analysis (Engineering Studio, Phase 20 UI).

    Returns 200 + plan content.
    Returns 404 when subject_ref does not exist.
    """
    subject_ref: str | None = body.get('subject_ref')
    if not subject_ref:
        raise HTTPException(status_code=422, detail='subject_ref is required')

    context_overrides: dict[str, Any] | None = body.get('context_overrides')

    with translate_service_errors({SubjectNotFoundError: (404, lambda e: f'Subject not found: {e.subject_ref}')}):
        plan = await service.create_plan(
            subject_ref=subject_ref,
            idempotency_key=None,
            context_overrides=context_overrides,
        )

    # Fetch items before rollback
    items_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == plan.id))
    items = list(items_result.scalars().all())

    response_dict = _plan_to_dict(plan, items)

    # Rollback — dry-run never persists
    await session.rollback()

    return response_dict


# -----------------------------------------------------------------------
# GET /plans
# -----------------------------------------------------------------------


@router.get('', status_code=200)
async def list_plans(
    subject_ref: str | None = None,
    subject_type: str | None = None,
    status: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = _DependsDB,
) -> dict[str, Any]:
    """List access plans with optional filters and pagination.

    Query params:
      subject_ref?: str     — filter by subject_ref
      subject_type?: str    — filter by subject_type (employee | nhi)
      status?: str          — filter by plan status
      limit?: int           — max rows (1..100, default 20)
      offset?: int          — row offset (default 0)
    """
    stmt = sa.select(AccessPlan).order_by(AccessPlan.created_at.desc())

    if subject_ref is not None:
        stmt = stmt.where(AccessPlan.subject_ref == subject_ref)
    if subject_type is not None:
        stmt = stmt.where(AccessPlan.subject_type == subject_type)
    if status is not None:
        try:
            status_enum = AccessPlanStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f'Invalid status: {status!r}') from None
        stmt = stmt.where(AccessPlan.status == status_enum)

    # Count total
    count_stmt = sa.select(sa.func.count()).select_from(stmt.subquery())
    total_result = await session.execute(count_stmt)
    total = total_result.scalar_one()

    # Paginate
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    plans = list(result.scalars().all())

    return {
        'items': [_plan_to_dict(p) for p in plans],
        'total': total,
        'limit': limit,
        'offset': offset,
    }


# -----------------------------------------------------------------------
# GET /plans/items/count  (MUST be before /{plan_id})
# -----------------------------------------------------------------------


def _parse_execution_statuses(raw: str | None) -> list[PlanItemExecutionStatus] | None:
    """Parse a CSV execution_status param like 'proposed,executing' into a list of enums.

    Returns None when raw is None (no filter). Raises HTTPException(422) on unknown value.
    """
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(',') if p.strip()]
    result: list[PlanItemExecutionStatus] = []
    for part in parts:
        try:
            result.append(PlanItemExecutionStatus(part))
        except ValueError:
            raise HTTPException(status_code=422, detail=f'Invalid execution_status value: {part!r}') from None
    return result or None


@router.get('/items/count', status_code=200, response_model=PlanItemCountResponse)
async def count_plan_items(
    execution_status: str | None = Query(default=None, description='CSV: proposed,executing,done,failed'),
    plan_status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    application: str | None = Query(default=None),
    plan_id: uuid.UUID | None = Query(default=None),
    subject_ref: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    session: AsyncSession = _DependsDB,
) -> PlanItemCountResponse:
    """Count PlanItems matching filters across all plans.

    Returns: {"count": N}
    """
    execution_statuses = _parse_execution_statuses(execution_status)

    kind_enum: PlanItemKind | None = None
    if kind is not None:
        try:
            kind_enum = PlanItemKind(kind)
        except ValueError:
            raise HTTPException(status_code=422, detail=f'Invalid kind: {kind!r}') from None

    total = await count_plan_items_cross_plan(
        session,
        execution_statuses=execution_statuses,
        plan_status=plan_status,
        kind=kind_enum,
        application=application,
        plan_id=plan_id,
        subject_ref=subject_ref,
        subject_type=subject_type,
    )
    return PlanItemCountResponse(count=total)


# -----------------------------------------------------------------------
# GET /plans/items  (MUST be before /{plan_id})
# -----------------------------------------------------------------------


@router.get('/items', status_code=200, response_model=PlanItemListResponse)
async def list_plan_items(
    execution_status: str | None = Query(default=None, description='CSV: proposed,executing,done,failed'),
    plan_status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    application: str | None = Query(default=None),
    plan_id: uuid.UUID | None = Query(default=None),
    subject_ref: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = _DependsDB,
) -> PlanItemListResponse:
    """Flat list of PlanItems with JOIN on PlanItemExecution and AccessPlan.

    Filters:
      execution_status  — CSV of proposed|executing|done|failed
      plan_status       — active|superseded|invalid|cancelled
      kind              — PlanItemKind value
      application       — filter by PlanItem.application (string match)
      plan_id           — filter by plan UUID
      subject_ref       — filter by AccessPlan.subject_ref
      subject_type      — filter by AccessPlan.subject_type
      limit             — 1..200, default 50
      offset            — default 0
    """
    execution_statuses = _parse_execution_statuses(execution_status)

    kind_enum: PlanItemKind | None = None
    if kind is not None:
        try:
            kind_enum = PlanItemKind(kind)
        except ValueError:
            raise HTTPException(status_code=422, detail=f'Invalid kind: {kind!r}') from None

    rows, total = await list_plan_items_cross_plan(
        session,
        execution_statuses=execution_statuses,
        plan_status=plan_status,
        kind=kind_enum,
        application=application,
        plan_id=plan_id,
        subject_ref=subject_ref,
        subject_type=subject_type,
        limit=limit,
        offset=offset,
    )

    raw_items = [
        PlanItemRead(
            id=row.id,
            plan_id=row.plan_id,
            plan_status=row.plan_status,
            subject_ref=row.subject_ref,
            subject_type=row.subject_type,
            kind=row.kind,
            application=row.application,
            account_ref=row.account_ref,
            target_descriptor=row.target_descriptor,
            initiatives=row.initiatives,
            initiative_refs=row.initiative_refs,
            policy_rule_refs=row.policy_rule_refs,
            decision_snapshot=row.decision_snapshot,
            execution_status=row.execution_status,
            failure_reason=row.failure_reason.value if row.failure_reason else None,
            last_verified_at=row.last_verified_at,
            last_error=row.last_error,
            created_at=row.created_at,
        )
        for row in rows
    ]

    items = await enrich_plan_items(session, raw_items)

    return PlanItemListResponse(items=items, total=total)


# -----------------------------------------------------------------------
# GET /plans/{id}
# -----------------------------------------------------------------------


@router.get('/{plan_id}', status_code=200)
async def get_plan(
    plan_id: uuid.UUID,
    session: AsyncSession = _DependsDB,
) -> dict[str, Any]:
    """Get a single access plan by id.

    Returns 200 + plan with items + executions.
    Returns 404 if plan not found.
    """
    plan_result = await session.execute(sa.select(AccessPlan).where(AccessPlan.id == plan_id))
    plan = plan_result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail=f'Plan {plan_id} not found')

    items_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == plan_id))
    items = list(items_result.scalars().all())

    deps_result = await session.execute(sa.select(PlanDependency).where(PlanDependency.plan_id == plan_id))
    deps = list(deps_result.scalars().all())

    exec_result = await session.execute(sa.select(PlanItemExecution).where(PlanItemExecution.plan_id == plan_id))
    executions = list(exec_result.scalars().all())

    response = _plan_to_dict(plan, items)
    response['dependencies'] = [
        {
            'item_id': str(d.item_id),
            'requires_item_id': str(d.requires_item_id),
        }
        for d in deps
    ]
    response['executions'] = [_execution_to_dict(e) for e in executions]

    return response


# -----------------------------------------------------------------------
# POST /plans/{id}/apply
# -----------------------------------------------------------------------


@router.post('/{plan_id}/apply')
async def apply_plan(
    plan_id: uuid.UUID,
    body: dict[str, Any] = Body(default={}),
    session: AsyncSession = _DependsDB,
    orchestrator: PipelineOrchestratorService = DependsOrchestratorService,
    pipelines: dict = DependsPipelines,
) -> JSONResponse:
    """Apply an access plan by creating a pipeline run.

    Query params:
      confirm_destructive?: bool — required when plan.requires_confirmation=True

    Returns:
      201  — new pipeline_run created for active plan
      200  — existing pipeline_run for same plan_id reused
      404  — plan not found
      409  — plan_not_active (superseded/invalid/cancelled) or
             apply_in_progress_for_subject (another plan of same subject applying)
      422  — destructive_threshold_exceeded (plan requires confirmation)
    """
    confirm_destructive: bool = body.get('confirm_destructive', False)

    # Step 1: SELECT plan
    plan_result = await session.execute(sa.select(AccessPlan).where(AccessPlan.id == plan_id))
    plan = plan_result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail=f'Plan {plan_id} not found')

    # Step 2: status check
    if plan.status != AccessPlanStatus.active:
        detail: dict[str, Any] = {
            'detail': f'Plan is not active: {plan.status.value}',
            'code': 'plan_not_active',
        }
        if plan.invalidation_reason is not None:
            detail['invalidation_reason'] = plan.invalidation_reason.value
        raise HTTPException(status_code=409, detail=detail)

    if plan.requires_confirmation and not confirm_destructive:
        raise HTTPException(
            status_code=422,
            detail={
                'detail': 'Plan requires confirmation of destructive changes',
                'code': 'destructive_threshold_exceeded',
            },
        )

    # Resolve pipeline definition (needed only when inserting — loaded here so all
    # paths that might need it have access; 503 is returned only when we actually
    # need it and it is missing).
    pipeline_def = pipelines.get(_ACCESS_APPLY_PIPELINE_NAME)

    # Step 3: INSERT into access_apply_active ON CONFLICT DO NOTHING
    new_run_id = uuid.uuid4()
    insert_stmt = sa.text(
        """
        INSERT INTO access_apply_active (subject_ref, subject_type, pipeline_run_id, plan_id, started_at)
        VALUES (:subject_ref, :subject_type, :pipeline_run_id, :plan_id, :started_at)
        ON CONFLICT (subject_ref) DO NOTHING
        RETURNING subject_ref
        """
    )
    insert_result = await session.execute(
        insert_stmt,
        {
            'subject_ref': plan.subject_ref,
            'subject_type': plan.subject_type,
            'pipeline_run_id': new_run_id,
            'plan_id': plan_id,
            'started_at': datetime.now(UTC),
        },
    )
    inserted_row = insert_result.fetchone()

    if inserted_row is not None:
        # Step 4: inserted → create pipeline run → 201
        if pipeline_def is None:
            # Clean up the lease we just inserted before erroring
            await session.execute(sa.delete(AccessApplyActive).where(AccessApplyActive.subject_ref == plan.subject_ref))
            await session.commit()
            raise HTTPException(
                status_code=503,
                detail=f'Pipeline {_ACCESS_APPLY_PIPELINE_NAME!r} not loaded',
            )
        run_result = await orchestrator.create_pipeline_run(
            pipeline_name=pipeline_def.name,
            pipeline_version=pipeline_def.version,
            args={'plan_id': str(plan_id)},
            trigger_source=PipelineTriggerSource.http,
        )

        # Update the lease row with the actual pipeline run id (the orchestrator may
        # have reused an existing in-flight run via content_hash dedup).
        actual_run_id = run_result.run.id
        if actual_run_id != new_run_id:
            # Update the lease with the real run_id
            await session.execute(
                sa.text('UPDATE access_apply_active SET pipeline_run_id = :run_id WHERE subject_ref = :subject_ref'),
                {'run_id': actual_run_id, 'subject_ref': plan.subject_ref},
            )

        await session.commit()

        status_code = 201 if run_result.created else 200
        return _apply_response(actual_run_id, status_code)

    # Step 5: conflict — existing row
    existing_result = await session.execute(
        sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == plan.subject_ref)
    )
    existing = existing_result.scalar_one_or_none()

    if existing is None:
        # Race: row was deleted between INSERT and SELECT → retry (one level)
        return await _retry_apply_insert(
            session=session,
            plan=plan,
            plan_id=plan_id,
            pipeline_def=pipeline_def,
            orchestrator=orchestrator,
        )

    existing_run_id = existing.pipeline_run_id
    existing_plan_id = existing.plan_id

    # Defensive: check if existing pipeline_run is terminal
    run_status = await get_pipeline_run_status(session, existing_run_id)
    if run_status is None or is_terminal(run_status):
        # Stale row → DELETE and retry
        await session.execute(sa.delete(AccessApplyActive).where(AccessApplyActive.subject_ref == plan.subject_ref))
        await session.flush()
        return await _retry_apply_insert(
            session=session,
            plan=plan,
            plan_id=plan_id,
            pipeline_def=pipeline_def,
            orchestrator=orchestrator,
        )

    # Active run exists
    if existing_plan_id == plan_id:
        # Same plan — idempotent: 200 + existing pipeline_run_id
        await session.commit()
        return _apply_response(existing_run_id, 200)

    # Different plan — another plan of same subject is in flight → 409
    await session.rollback()
    raise HTTPException(
        status_code=409,
        detail={
            'detail': 'Another plan for this subject is currently being applied',
            'code': 'apply_in_progress_for_subject',
            'existing_pipeline_run_id': str(existing_run_id),
            'existing_plan_id': str(existing_plan_id),
        },
    )


# -----------------------------------------------------------------------
# Helpers for apply
# -----------------------------------------------------------------------


def _apply_response(pipeline_run_id: uuid.UUID, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={'pipeline_run_id': str(pipeline_run_id)},
    )


async def _retry_apply_insert(
    *,
    session: AsyncSession,
    plan: AccessPlan,
    plan_id: uuid.UUID,
    pipeline_def: Any,
    orchestrator: PipelineOrchestratorService,
) -> JSONResponse:
    """Single retry of the INSERT after a stale-row cleanup."""
    new_run_id = uuid.uuid4()
    insert_stmt = sa.text(
        """
        INSERT INTO access_apply_active (subject_ref, subject_type, pipeline_run_id, plan_id, started_at)
        VALUES (:subject_ref, :subject_type, :pipeline_run_id, :plan_id, :started_at)
        ON CONFLICT (subject_ref) DO NOTHING
        RETURNING subject_ref
        """
    )
    insert_result = await session.execute(
        insert_stmt,
        {
            'subject_ref': plan.subject_ref,
            'subject_type': plan.subject_type,
            'pipeline_run_id': new_run_id,
            'plan_id': plan_id,
            'started_at': datetime.now(UTC),
        },
    )
    inserted_row = insert_result.fetchone()

    if inserted_row is None:
        # Still conflicting — another apply is racing; give a 409
        existing_result = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == plan.subject_ref)
        )
        existing = existing_result.scalar_one_or_none()
        await session.rollback()
        detail: dict[str, Any] = {
            'detail': 'Another plan for this subject is currently being applied',
            'code': 'apply_in_progress_for_subject',
        }
        if existing is not None:
            detail['existing_pipeline_run_id'] = str(existing.pipeline_run_id)
            detail['existing_plan_id'] = str(existing.plan_id)
        raise HTTPException(status_code=409, detail=detail)

    run_result = await orchestrator.create_pipeline_run(
        pipeline_name=pipeline_def.name,
        pipeline_version=pipeline_def.version,
        args={'plan_id': str(plan_id)},
        trigger_source=PipelineTriggerSource.http,
    )
    actual_run_id = run_result.run.id
    if actual_run_id != new_run_id:
        await session.execute(
            sa.text('UPDATE access_apply_active SET pipeline_run_id = :run_id WHERE subject_ref = :subject_ref'),
            {'run_id': actual_run_id, 'subject_ref': plan.subject_ref},
        )

    await session.commit()
    status_code = 201 if run_result.created else 200
    return _apply_response(actual_run_id, status_code)
