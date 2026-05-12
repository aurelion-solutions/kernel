# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pipeline orchestrator REST routes (Step 11).

Two APIRouter instances:
- ``router``            — /pipelines and /pipeline-runs
- ``well_known_router`` — /.well-known/pipeline-schema.json and /.well-known/pipeline-actions.json

Bootstrap discipline: no load_dotenv, no get_settings(), no DB access at import.
"""

from __future__ import annotations

from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from jsonschema import Draft202012Validator
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.platform.logs.deps import get_log_service
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import merge_emit_component_trace_fields
from src.platform.orchestrator.deps import (
    get_loaded_pipelines,
    get_orchestrator_service,
    get_pipeline_loader,
)
from src.platform.orchestrator.discovery import build_action_catalogue, build_merged_pipeline_schema
from src.platform.orchestrator.loader import PipelineDefinition
from src.platform.orchestrator.models import (
    PipelineRun,
    PipelineRunStatus,
    PipelineTriggerSource,
    StepRun,
)
from src.platform.orchestrator.registry import ACTION_REGISTRY
from src.platform.orchestrator.schemas import (
    ActionDescriptor,
    CancelPipelineRunResponse,
    CreatePipelineRunRequest,
    CreatePipelineRunResponse,
    PipelineDetail,
    PipelineRunDetail,
    PipelineRunSummary,
    PipelineSummary,
    PipelineTriggerSpec,
    RetryPipelineRunResponse,
    StepRunDetail,
    StepRunSummary,
)
from src.platform.orchestrator.service import PipelineOrchestratorService
from src.platform.orchestrator.service_types import (
    AlreadyCancellingError,
    OrchestratorRowMissing,
    OrchestratorStateConflict,
    RunNotRetryableError,
    TerminalStatusError,
)

router = APIRouter(tags=['orchestrator'])
well_known_router = APIRouter(prefix='/.well-known', tags=['orchestrator-discovery'])

_COMPONENT = 'orchestrator_routes'

# ---------------------------------------------------------------------------
# Pre-built Depends aliases
# ---------------------------------------------------------------------------

DependsSession = Depends(get_db)
DependsService = Depends(get_orchestrator_service)
DependsLoader = Depends(get_pipeline_loader)
DependsPipelines = Depends(get_loaded_pipelines)

_q_limit = Query(50, ge=1, le=200, description='Max rows (1..200).')
_q_offset = Query(0, ge=0, description='Row offset for pagination.')
_q_status = Query(None, description='Filter by pipeline run status (repeatable).')
_q_pipeline_name = Query(None, description='Filter by pipeline name.')


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _pipeline_summary(defn: PipelineDefinition) -> PipelineSummary:
    """Convert a loaded PipelineDefinition to PipelineSummary."""
    description: str | None = None
    pipeline_dict = defn.raw_dict.get('pipeline', {})
    if isinstance(pipeline_dict, dict):
        description = pipeline_dict.get('description')

    triggers = [PipelineTriggerSpec(**dict(t)) for t in defn.triggers]
    return PipelineSummary(
        name=defn.name,
        version=defn.version,
        schema_version=defn.schema_version,
        description=description,
        step_count=len(defn.steps),
        triggers=triggers,
    )


def _pipeline_detail(defn: PipelineDefinition) -> PipelineDetail:
    """Convert a loaded PipelineDefinition to PipelineDetail."""
    summary = _pipeline_summary(defn)
    return PipelineDetail(
        **summary.model_dump(),
        args_schema=defn.args_schema_dict,
        steps=[dict(s) for s in defn.steps],
        content_hash=defn.content_hash,
        source_path=str(defn.source_path),
    )


def _step_summary(step: StepRun) -> StepRunSummary:
    return StepRunSummary(
        id=step.id,
        step_name=step.step_name,
        attempt=step.attempt,
        status=step.status,
        started_at=step.started_at,
        finished_at=step.finished_at,
        error=step.error,
    )


def _run_summary(run: PipelineRun) -> PipelineRunSummary:
    return PipelineRunSummary(
        id=run.id,
        pipeline_name=run.pipeline_name,
        pipeline_version=run.pipeline_version,
        content_hash=run.content_hash,
        status=run.status,
        trigger_source=run.trigger_source.value,
        current_step=run.current_step,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _run_detail(run: PipelineRun, steps: list[StepRun]) -> PipelineRunDetail:
    ordered_steps = sorted(steps, key=lambda s: (s.created_at, s.attempt))
    return PipelineRunDetail(
        id=run.id,
        pipeline_name=run.pipeline_name,
        pipeline_version=run.pipeline_version,
        content_hash=run.content_hash,
        status=run.status,
        trigger_source=run.trigger_source.value,
        current_step=run.current_step,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        created_at=run.created_at,
        updated_at=run.updated_at,
        args=run.args,
        steps=[_step_summary(s) for s in ordered_steps],
    )


# ---------------------------------------------------------------------------
# Pipeline definition routes
# ---------------------------------------------------------------------------


@router.get('/pipelines', response_model=list[PipelineSummary])
async def list_pipelines(
    pipelines: dict[str, PipelineDefinition] = DependsPipelines,
) -> list[PipelineSummary]:
    """List all loaded pipeline definitions."""
    return [_pipeline_summary(defn) for defn in pipelines.values()]


@router.get('/pipelines/{name}', response_model=PipelineDetail)
async def get_pipeline(
    name: str,
    pipelines: dict[str, PipelineDefinition] = DependsPipelines,
) -> PipelineDetail:
    """Return full details for a single loaded pipeline definition."""
    defn = pipelines.get(name)
    if defn is None:
        raise HTTPException(status_code=404, detail=f'Pipeline {name!r} not loaded')
    return _pipeline_detail(defn)


# ---------------------------------------------------------------------------
# Pipeline run routes
# ---------------------------------------------------------------------------


@router.get('/pipeline-runs', response_model=list[PipelineRunSummary])
async def list_pipeline_runs(
    session: AsyncSession = DependsSession,
    limit: int = _q_limit,
    offset: int = _q_offset,
    status: list[PipelineRunStatus] | None = _q_status,
    pipeline_name: str | None = _q_pipeline_name,
) -> list[PipelineRunSummary]:
    """List pipeline runs with optional status/name filters, newest first."""
    q = sa.select(PipelineRun)
    if status:
        q = q.where(PipelineRun.status.in_(status))
    if pipeline_name is not None:
        q = q.where(PipelineRun.pipeline_name == pipeline_name)
    q = (
        q.order_by(
            sa.nulls_last(PipelineRun.started_at.desc()),
            PipelineRun.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(q)
    rows = result.scalars().all()
    return [_run_summary(r) for r in rows]


@router.get('/pipeline-runs/{run_id}', response_model=PipelineRunDetail)
async def get_pipeline_run(
    run_id: uuid.UUID,
    session: AsyncSession = DependsSession,
) -> PipelineRunDetail:
    """Return full details for a single pipeline run, including ordered step runs."""
    run = await session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='Pipeline run not found')

    steps_result = await session.execute(
        sa.select(StepRun)
        .where(StepRun.pipeline_run_id == run_id)
        .order_by(StepRun.created_at.asc(), StepRun.attempt.asc())
    )
    steps = list(steps_result.scalars().all())
    return _run_detail(run, steps)


@router.get(
    '/pipeline-runs/{run_id}/steps/{step_name}',
    response_model=StepRunDetail,
)
async def get_step_run(
    run_id: uuid.UUID,
    step_name: str,
    session: AsyncSession = DependsSession,
) -> StepRunDetail:
    """Return the latest attempt for a named step within a pipeline run."""
    run = await session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='Pipeline run not found')

    result = await session.execute(
        sa.select(StepRun)
        .where(StepRun.pipeline_run_id == run_id, StepRun.step_name == step_name)
        .order_by(StepRun.attempt.desc())
        .limit(1)
    )
    step = result.scalar_one_or_none()
    if step is None:
        raise HTTPException(status_code=404, detail=f'Step {step_name!r} not found in run {run_id}')

    return StepRunDetail(
        id=step.id,
        step_name=step.step_name,
        attempt=step.attempt,
        status=step.status,
        started_at=step.started_at,
        finished_at=step.finished_at,
        error=step.error,
        args=step.args,
        result=step.result,
    )


@router.post('/pipeline-runs', status_code=201)
async def create_pipeline_run(
    body: CreatePipelineRunRequest,
    request: Request,
    response: Response,
    session: AsyncSession = DependsSession,
    service: PipelineOrchestratorService = DependsService,
    pipelines: dict[str, PipelineDefinition] = DependsPipelines,
) -> CreatePipelineRunResponse:
    """Manually trigger a pipeline run.

    Returns 201 on fresh insert (``created=True``), 200 on idempotent duplicate
    (``created=False``).  Run sits in ``pending`` until Step 12 (runner).

    Single-version-per-name invariant: if ``pipeline_version`` is supplied and
    mismatches the loaded definition → 404.  Multi-version coexistence is out of
    scope — Step 14 (hot reload) decides whether to key by (name, version).
    """
    log_service = get_log_service(request)
    correlation_id: str | None = getattr(request.state, 'correlation_id', None)

    # 1. Resolve pipeline definition.
    defn = pipelines.get(body.pipeline_name)
    if defn is None:
        raise HTTPException(status_code=404, detail=f'Pipeline {body.pipeline_name!r} not loaded')

    # 2. Version check.
    resolved_version = defn.version
    if body.pipeline_version is not None and body.pipeline_version != defn.version:
        raise HTTPException(
            status_code=404,
            detail=(f'Pipeline version {body.pipeline_version} not loaded (current: {defn.version})'),
        )

    # 3. Validate args against pipeline args schema.
    if defn.args_schema_dict:
        validator = Draft202012Validator(defn.args_schema_dict)
        errors = list(validator.iter_errors(body.args))
        if errors:
            raise HTTPException(status_code=422, detail=errors[0].message)

    # 4. Emit INFO log before service call.
    log_service.emit_safe(
        level=LogLevel.INFO,
        message='Pipeline run create requested',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {
                'pipeline_name': body.pipeline_name,
                'pipeline_version': resolved_version,
            },
            component_id=_COMPONENT,
            target_id=body.pipeline_name,
        ),
        correlation_id=correlation_id,
    )

    # 5. Call service.
    with translate_service_errors(
        {
            OrchestratorStateConflict: (409, 'Pipeline run state conflict'),
            OrchestratorRowMissing: (404, 'Pipeline run not found'),
        }
    ):
        result = await service.create_pipeline_run(
            body.pipeline_name,
            resolved_version,
            body.args,
            PipelineTriggerSource.http,
            correlation_id=correlation_id,
        )

    await session.commit()

    # 6. Set status code based on whether a new row was created.
    if not result.created:
        response.status_code = 200

    return CreatePipelineRunResponse(
        pipeline_run_id=result.run.id,
        status=result.run.status,
        pipeline_version=result.run.pipeline_version,
        created=result.created,
    )


@router.post('/pipeline-runs/{run_id}/cancel', response_model=CancelPipelineRunResponse)
async def cancel_pipeline_run(
    run_id: uuid.UUID,
    request: Request,
    session: AsyncSession = DependsSession,
    service: PipelineOrchestratorService = DependsService,
) -> CancelPipelineRunResponse:
    """Request cancellation of a pipeline run.

    Returns 200 with status='cancelled' when the run was cancelled synchronously
    (pending or awaiting_event branch) or status='cancelling' when the runner
    watcher owns the terminal transition (running branch).

    Error responses:
    - 404 — run not found.
    - 409 — run is already cancelling or in a terminal status.
    """
    log_service = get_log_service(request)
    correlation_id: str | None = getattr(request.state, 'correlation_id', None)

    log_service.emit_safe(  # allowed-emit-safe: observability
        level=LogLevel.INFO,
        message='Pipeline run cancel requested',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {'run_id': str(run_id)},
            component_id=_COMPONENT,
            target_id=str(run_id),
        ),
        correlation_id=correlation_id,
    )

    def _terminal_detail(exc: Exception) -> str:
        assert isinstance(exc, TerminalStatusError)
        return f'Pipeline run is in terminal status: {exc.status.value}'

    with translate_service_errors(
        {
            OrchestratorRowMissing: (404, 'Pipeline run not found'),
            AlreadyCancellingError: (409, 'Pipeline run is already cancelling'),
            TerminalStatusError: (409, _terminal_detail),
        }
    ):
        outcome = await service.request_cancel(run_id, correlation_id=correlation_id)

    await session.commit()

    # outcome.status is always cancelled or cancelling by contract of request_cancel.
    assert outcome.status in (PipelineRunStatus.cancelled, PipelineRunStatus.cancelling)
    return CancelPipelineRunResponse(
        run_id=outcome.run_id,
        status=outcome.status,  # type: ignore[arg-type]
    )


@router.post(
    '/pipeline-runs/{run_id}/retry',
    status_code=201,
    response_model=RetryPipelineRunResponse,
)
async def retry_pipeline_run(
    run_id: uuid.UUID,
    request: Request,
    session: AsyncSession = DependsSession,
    service: PipelineOrchestratorService = DependsService,
) -> RetryPipelineRunResponse:
    """Create a retry of a completed/failed/cancelled pipeline run.

    Returns 201 on success, 404 if the source run is not found,
    409 if the source run is not in a terminal status.
    """
    log_service = get_log_service(request)
    correlation_id: str | None = getattr(request.state, 'correlation_id', None)

    log_service.emit_safe(  # allowed-emit-safe: observability
        level=LogLevel.INFO,
        message='Pipeline run retry requested',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {'run_id': str(run_id)},
            component_id=_COMPONENT,
            target_id=str(run_id),
        ),
        correlation_id=correlation_id,
    )

    def _not_retryable_detail(exc: Exception) -> str:
        assert isinstance(exc, RunNotRetryableError)
        if exc.reason == 'cancelling':
            return 'Pipeline run is cancelling - wait for it to settle'
        return f'Pipeline run is not in a terminal status: {exc.status.value}'

    with translate_service_errors(
        {
            OrchestratorRowMissing: (404, 'Pipeline run not found'),
            RunNotRetryableError: (409, _not_retryable_detail),
        }
    ):
        result = await service.create_retry(run_id, correlation_id=correlation_id)

    await session.commit()

    new_run = result.run
    assert new_run.retry_of_run_id is not None  # service contract
    return RetryPipelineRunResponse(
        run_id=new_run.id,
        retry_of_run_id=new_run.retry_of_run_id,
        status=new_run.status,
        pipeline_name=new_run.pipeline_name,
        pipeline_version=new_run.pipeline_version,
    )


# ---------------------------------------------------------------------------
# Well-known discovery routes
# ---------------------------------------------------------------------------


@well_known_router.get('/pipeline-schema.json')
async def get_pipeline_schema(
    request: Request,
) -> dict[str, Any]:
    """Return the merged pipeline YAML grammar with per-action arg/result schemas injected.

    Merge is ADDITIVE — existing $defs entries are not overwritten; action schemas
    are injected under $defs.action_args and $defs.action_results only.
    See discovery.py for the rationale.
    """
    log_service = get_log_service(request)
    actions = ACTION_REGISTRY.all()
    return build_merged_pipeline_schema(actions, log_service=log_service)


@well_known_router.get('/pipeline-actions.json', response_model=list[ActionDescriptor])
async def get_pipeline_actions() -> list[ActionDescriptor]:
    """Return the full registered action catalogue."""
    return build_action_catalogue(ACTION_REGISTRY.all())
