# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API tests for access_plan REST endpoints (Phase 19 Step E1).

Covers:
- POST /plans: 201 new plan, 404 subject not found, 422 missing subject_ref
- POST /plans/dry-run: 200, 404, no persistence
- GET /plans: 200 with pagination, status filter
- GET /plans/{id}: 200, 404
- POST /plans/{id}/apply:
  - 201 new pipeline run
  - 200 reuse same plan apply
  - 404 plan not found
  - 409 plan_not_active (superseded, invalid, cancelled)
  - 409 apply_in_progress_for_subject (other plan active)
  - 422 destructive_threshold_exceeded
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.engines.access_plan.deps import (
    get_access_plan_service,
    get_plan_orchestrator_service,
    get_plan_pipelines,
)
from src.engines.access_plan.models import (
    AccessPlan,
    AccessPlanStatus,
    PlanInvalidationReason,
)
from src.engines.access_plan.routes import router
from src.engines.access_plan.service import AccessPlanService, SubjectNotFoundError
from src.platform.orchestrator.loader import PipelineDefinition
from src.platform.orchestrator.models import PipelineRunStatus
from src.platform.orchestrator.service import PipelineOrchestratorService
from src.platform.orchestrator.service_types import PipelineRunCreateResult

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix='/api/v0')
    return app


# ---------------------------------------------------------------------------
# Fake model builders
# ---------------------------------------------------------------------------


def _fake_plan(
    status: AccessPlanStatus = AccessPlanStatus.active,
    requires_confirmation: bool = False,
    plan_id: uuid.UUID | None = None,
    subject_ref: str | None = None,
    invalidation_reason: PlanInvalidationReason | None = None,
) -> MagicMock:
    plan = MagicMock(spec=AccessPlan)
    plan.id = plan_id or uuid.uuid4()
    plan.subject_ref = subject_ref or str(uuid.uuid4())
    plan.subject_type = 'employee'
    plan.status = status
    plan.requires_confirmation = requires_confirmation
    plan.content_hash = 'abc123'
    plan.created_at = datetime.now(UTC)
    plan.supersedes_plan_id = None
    plan.invalidation_reason = invalidation_reason
    plan.invalidated_by_plan_id = None
    plan.idempotency_key = None
    return plan


def _fake_pipeline_run(run_id: uuid.UUID | None = None) -> MagicMock:
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.pipeline_name = 'access_apply_pipeline'
    run.pipeline_version = 1
    return run


def _fake_pipeline_def() -> PipelineDefinition:
    from pathlib import Path

    return PipelineDefinition(
        name='access_apply_pipeline',
        version=1,
        schema_version=1,
        source_path=Path('/fake/access_apply_pipeline.yaml'),
        content_hash='fake_hash',
        args_schema_dict={},
        steps=(),
        triggers=(),
        raw_dict={},
    )


def _make_service_mock(plan: AccessPlan | None = None, raises: Exception | None = None) -> MagicMock:
    svc = AsyncMock(spec=AccessPlanService)
    if raises is not None:
        svc.create_plan.side_effect = raises
    else:
        svc.create_plan.return_value = plan or _fake_plan()
    return svc


def _make_orchestrator_mock(run: MagicMock | None = None, created: bool = True) -> MagicMock:
    orch = AsyncMock(spec=PipelineOrchestratorService)
    fake_run = run or _fake_pipeline_run()
    orch.create_pipeline_run.return_value = PipelineRunCreateResult(run=fake_run, created=created)
    return orch


# ---------------------------------------------------------------------------
# Helpers: DB session mock that returns empty scalars by default
# ---------------------------------------------------------------------------


def _make_session_mock() -> AsyncMock:
    session = AsyncMock()
    # Default: empty result set
    result_mock = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock.scalars.return_value = scalars_mock
    result_mock.scalar_one_or_none.return_value = None
    result_mock.scalar_one.return_value = 0
    result_mock.fetchone.return_value = None
    result_mock.all.return_value = []
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Tests: POST /plans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_plans_201_new_plan() -> None:
    """POST /plans returns 201 with plan content."""
    plan = _fake_plan()
    svc_mock = _make_service_mock(plan=plan)
    session_mock = _make_session_mock()

    app = _make_app()
    app.dependency_overrides[get_access_plan_service] = lambda session=None: svc_mock
    app.dependency_overrides[get_db] = lambda: session_mock

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.post(
            '/api/v0/plans',
            json={'subject_ref': plan.subject_ref},
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data['id'] == str(plan.id)
    assert data['subject_ref'] == plan.subject_ref
    assert data['status'] == 'active'
    assert 'items' in data


@pytest.mark.asyncio
async def test_post_plans_404_subject_not_found() -> None:
    """POST /plans returns 404 when subject_ref does not exist."""
    svc_mock = _make_service_mock(raises=SubjectNotFoundError('no-such-subject'))

    app = _make_app()
    app.dependency_overrides[get_access_plan_service] = lambda session=None: svc_mock
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.post('/api/v0/plans', json={'subject_ref': 'no-such-subject'})

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_plans_422_missing_subject_ref() -> None:
    """POST /plans returns 422 when subject_ref is missing."""
    app = _make_app()
    app.dependency_overrides[get_access_plan_service] = lambda: AsyncMock()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.post('/api/v0/plans', json={})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: POST /plans/dry-run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_plans_dry_run_200_no_persist() -> None:
    """POST /plans/dry-run returns 200 and rolls back transaction."""
    plan = _fake_plan()
    svc_mock = _make_service_mock(plan=plan)
    session_mock = _make_session_mock()

    app = _make_app()
    app.dependency_overrides[get_access_plan_service] = lambda session=None: svc_mock
    app.dependency_overrides[get_db] = lambda: session_mock

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.post('/api/v0/plans/dry-run', json={'subject_ref': plan.subject_ref})

    assert resp.status_code == 200
    data = resp.json()
    assert data['id'] == str(plan.id)
    # Rollback was called, not commit
    session_mock.rollback.assert_called()
    session_mock.commit.assert_not_called()


@pytest.mark.asyncio
async def test_post_plans_dry_run_404_subject_not_found() -> None:
    """POST /plans/dry-run returns 404 when subject_ref does not exist."""
    svc_mock = _make_service_mock(raises=SubjectNotFoundError('no-such'))

    app = _make_app()
    app.dependency_overrides[get_access_plan_service] = lambda session=None: svc_mock
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.post('/api/v0/plans/dry-run', json={'subject_ref': 'no-such'})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: GET /plans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_plans_200_empty_list() -> None:
    """GET /plans returns 200 with empty list."""
    session_mock = _make_session_mock()

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.get('/api/v0/plans')

    assert resp.status_code == 200
    data = resp.json()
    assert data['items'] == []
    assert data['total'] == 0


@pytest.mark.asyncio
async def test_get_plans_422_invalid_status() -> None:
    """GET /plans?status=bogus returns 422."""
    session_mock = _make_session_mock()

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.get('/api/v0/plans?status=bogus_value')

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: GET /plans/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_plan_by_id_404() -> None:
    """GET /plans/{id} returns 404 for unknown plan."""
    session_mock = _make_session_mock()

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.get(f'/api/v0/plans/{uuid.uuid4()}')

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_plan_by_id_200() -> None:
    """GET /plans/{id} returns 200 with plan + items + deps + executions."""
    plan = _fake_plan()
    session_mock = _make_session_mock()

    # Plan select → return plan
    plan_result = MagicMock()
    plan_result.scalar_one_or_none.return_value = plan

    # Items select → empty
    items_result = MagicMock()
    items_scalars = MagicMock()
    items_scalars.all.return_value = []
    items_result.scalars.return_value = items_scalars

    # Deps select → empty
    deps_result = MagicMock()
    deps_scalars = MagicMock()
    deps_scalars.all.return_value = []
    deps_result.scalars.return_value = deps_scalars

    # Executions select → empty
    exec_result = MagicMock()
    exec_scalars = MagicMock()
    exec_scalars.all.return_value = []
    exec_result.scalars.return_value = exec_scalars

    session_mock.execute = AsyncMock(side_effect=[plan_result, items_result, deps_result, exec_result])

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.get(f'/api/v0/plans/{plan.id}')

    assert resp.status_code == 200
    data = resp.json()
    assert data['id'] == str(plan.id)
    assert 'dependencies' in data
    assert 'executions' in data


# ---------------------------------------------------------------------------
# Tests: POST /plans/{id}/apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_plan_404_not_found() -> None:
    """POST /plans/{id}/apply returns 404 if plan not found."""
    session_mock = _make_session_mock()

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock
    app.dependency_overrides[get_plan_orchestrator_service] = lambda: AsyncMock()
    app.dependency_overrides[get_plan_pipelines] = lambda: {}

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.post(f'/api/v0/plans/{uuid.uuid4()}/apply', json={})

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_apply_plan_409_plan_not_active_superseded() -> None:
    """POST /plans/{id}/apply returns 409 plan_not_active for superseded plan."""
    plan = _fake_plan(status=AccessPlanStatus.superseded)
    session_mock = _make_session_mock()

    plan_result = MagicMock()
    plan_result.scalar_one_or_none.return_value = plan
    session_mock.execute = AsyncMock(return_value=plan_result)

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock
    app.dependency_overrides[get_plan_orchestrator_service] = lambda: AsyncMock()
    app.dependency_overrides[get_plan_pipelines] = lambda: {}

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.post(f'/api/v0/plans/{plan.id}/apply', json={})

    assert resp.status_code == 409
    data = resp.json()
    assert data['detail']['code'] == 'plan_not_active'


@pytest.mark.asyncio
async def test_apply_plan_409_plan_not_active_invalid() -> None:
    """POST /plans/{id}/apply returns 409 plan_not_active for invalid plan."""
    plan = _fake_plan(
        status=AccessPlanStatus.invalid,
        invalidation_reason=PlanInvalidationReason.stale_after_apply,
    )
    session_mock = _make_session_mock()

    plan_result = MagicMock()
    plan_result.scalar_one_or_none.return_value = plan
    session_mock.execute = AsyncMock(return_value=plan_result)

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock
    app.dependency_overrides[get_plan_orchestrator_service] = lambda: AsyncMock()
    app.dependency_overrides[get_plan_pipelines] = lambda: {}

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.post(f'/api/v0/plans/{plan.id}/apply', json={})

    assert resp.status_code == 409
    data = resp.json()
    assert data['detail']['code'] == 'plan_not_active'
    assert data['detail']['invalidation_reason'] == 'stale_after_apply'


@pytest.mark.asyncio
async def test_apply_plan_422_requires_confirmation() -> None:
    """POST /plans/{id}/apply returns 422 when requires_confirmation=True without flag."""
    plan = _fake_plan(requires_confirmation=True)
    session_mock = _make_session_mock()

    plan_result = MagicMock()
    plan_result.scalar_one_or_none.return_value = plan
    session_mock.execute = AsyncMock(return_value=plan_result)

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock
    app.dependency_overrides[get_plan_orchestrator_service] = lambda: AsyncMock()
    app.dependency_overrides[get_plan_pipelines] = lambda: {}

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.post(f'/api/v0/plans/{plan.id}/apply', json={})

    assert resp.status_code == 422
    data = resp.json()
    assert data['detail']['code'] == 'destructive_threshold_exceeded'


@pytest.mark.asyncio
async def test_apply_plan_422_with_confirm_passes() -> None:
    """POST /plans/{id}/apply passes when confirm_destructive=true."""
    plan = _fake_plan(requires_confirmation=True)
    run_id = uuid.uuid4()
    pipeline_def = _fake_pipeline_def()
    orch_mock = _make_orchestrator_mock(run=_fake_pipeline_run(run_id=run_id), created=True)

    session_mock = _make_session_mock()

    # Simulate: plan found, INSERT returns a row (success)
    plan_result = MagicMock()
    plan_result.scalar_one_or_none.return_value = plan

    insert_result = MagicMock()
    insert_result.fetchone.return_value = ('some_subject_ref',)  # non-None → inserted

    # Second execute call (update lease with actual run_id)
    update_result = MagicMock()

    session_mock.execute = AsyncMock(side_effect=[plan_result, insert_result, update_result])

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock
    app.dependency_overrides[get_plan_orchestrator_service] = lambda: orch_mock
    app.dependency_overrides[get_plan_pipelines] = lambda: {'access_apply_pipeline': pipeline_def}

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.post(
            f'/api/v0/plans/{plan.id}/apply',
            json={'confirm_destructive': True},
        )

    assert resp.status_code == 201
    data = resp.json()
    assert 'pipeline_run_id' in data


@pytest.mark.asyncio
async def test_apply_plan_201_new_run() -> None:
    """POST /plans/{id}/apply returns 201 with pipeline_run_id for a new run."""
    plan = _fake_plan()
    run_id = uuid.uuid4()
    pipeline_def = _fake_pipeline_def()
    orch_mock = _make_orchestrator_mock(run=_fake_pipeline_run(run_id=run_id), created=True)

    session_mock = _make_session_mock()
    plan_result = MagicMock()
    plan_result.scalar_one_or_none.return_value = plan

    insert_result = MagicMock()
    insert_result.fetchone.return_value = (plan.subject_ref,)  # inserted

    update_result = MagicMock()
    session_mock.execute = AsyncMock(side_effect=[plan_result, insert_result, update_result])

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock
    app.dependency_overrides[get_plan_orchestrator_service] = lambda: orch_mock
    app.dependency_overrides[get_plan_pipelines] = lambda: {'access_apply_pipeline': pipeline_def}

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.post(f'/api/v0/plans/{plan.id}/apply', json={})

    assert resp.status_code == 201
    data = resp.json()
    assert data['pipeline_run_id'] == str(run_id)


@pytest.mark.asyncio
async def test_apply_plan_200_reuse_same_plan() -> None:
    """POST /plans/{id}/apply returns 200 for same plan with active run (idempotent)."""
    plan = _fake_plan()
    run_id = uuid.uuid4()

    session_mock = _make_session_mock()
    plan_result = MagicMock()
    plan_result.scalar_one_or_none.return_value = plan

    # INSERT returns nothing → conflict
    insert_result = MagicMock()
    insert_result.fetchone.return_value = None

    # SELECT AccessApplyActive → returns existing row with same plan_id
    existing_lease = MagicMock()
    existing_lease.pipeline_run_id = run_id
    existing_lease.plan_id = plan.id
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing_lease

    # get_pipeline_run_status call → non-terminal
    status_result = MagicMock()
    status_result.scalar_one_or_none.return_value = PipelineRunStatus.running

    session_mock.execute = AsyncMock(side_effect=[plan_result, insert_result, existing_result, status_result])

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock
    app.dependency_overrides[get_plan_orchestrator_service] = lambda: AsyncMock()
    app.dependency_overrides[get_plan_pipelines] = lambda: {}

    with patch(
        'src.engines.access_plan.routes.get_pipeline_run_status',
        new=AsyncMock(return_value=PipelineRunStatus.running),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.post(f'/api/v0/plans/{plan.id}/apply', json={})

    assert resp.status_code == 200
    data = resp.json()
    assert data['pipeline_run_id'] == str(run_id)


@pytest.mark.asyncio
async def test_apply_plan_409_apply_in_progress_for_subject() -> None:
    """POST /plans/{id}/apply returns 409 apply_in_progress_for_subject for a different plan."""
    plan = _fake_plan()
    other_plan_id = uuid.uuid4()
    other_run_id = uuid.uuid4()

    session_mock = _make_session_mock()
    plan_result = MagicMock()
    plan_result.scalar_one_or_none.return_value = plan

    insert_result = MagicMock()
    insert_result.fetchone.return_value = None  # conflict

    existing_lease = MagicMock()
    existing_lease.pipeline_run_id = other_run_id
    existing_lease.plan_id = other_plan_id  # different plan
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing_lease

    session_mock.execute = AsyncMock(side_effect=[plan_result, insert_result, existing_result])

    app = _make_app()
    app.dependency_overrides[get_db] = lambda: session_mock
    app.dependency_overrides[get_plan_orchestrator_service] = lambda: AsyncMock()
    app.dependency_overrides[get_plan_pipelines] = lambda: {}

    with patch(
        'src.engines.access_plan.routes.get_pipeline_run_status',
        new=AsyncMock(return_value=PipelineRunStatus.running),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.post(f'/api/v0/plans/{plan.id}/apply', json={})

    assert resp.status_code == 409
    data = resp.json()
    assert data['detail']['code'] == 'apply_in_progress_for_subject'
    assert data['detail']['existing_pipeline_run_id'] == str(other_run_id)
    assert data['detail']['existing_plan_id'] == str(other_plan_id)


# ---------------------------------------------------------------------------
# Tests: action registration
# ---------------------------------------------------------------------------


def test_access_plan_plan_action_registered() -> None:
    """access_plan.plan action is registered in ACTION_REGISTRY."""
    from src.engines.access_plan.actions import _ensure_plan_registered  # noqa: PLC0415
    from src.platform.orchestrator.registry import ACTION_REGISTRY, ActionNotFoundError

    _ensure_plan_registered()
    try:
        ACTION_REGISTRY.get('access_plan', 'plan')
    except ActionNotFoundError:
        pytest.fail('access_plan.plan is not registered in ACTION_REGISTRY')


def test_access_apply_execute_plan_action_registered() -> None:
    """access_apply.execute_plan action is registered in ACTION_REGISTRY."""
    from src.engines.access_apply.actions import _ensure_execute_plan_registered  # noqa: PLC0415
    from src.platform.orchestrator.registry import ACTION_REGISTRY, ActionNotFoundError

    _ensure_execute_plan_registered()
    try:
        ACTION_REGISTRY.get('access_apply', 'execute_plan')
    except ActionNotFoundError:
        pytest.fail('access_apply.execute_plan is not registered in ACTION_REGISTRY')


def test_inventory_initiatives_scan_for_replan_action_registered() -> None:
    """inventory.initiatives.scan_for_replan action is registered in ACTION_REGISTRY."""
    from src.inventory.initiatives.actions import _ensure_scan_for_replan_registered  # noqa: PLC0415
    from src.platform.orchestrator.registry import ACTION_REGISTRY, ActionNotFoundError

    # Re-register if the ACTION_REGISTRY was cleared by a prior _registry_isolation
    # fixture (engine test_actions modules call _clear_for_tests() as teardown).
    _ensure_scan_for_replan_registered()

    try:
        ACTION_REGISTRY.get('inventory.initiatives', 'scan_for_replan')
    except ActionNotFoundError:
        pytest.fail('inventory.initiatives.scan_for_replan is not registered in ACTION_REGISTRY')


# ---------------------------------------------------------------------------
# Tests: pipeline YAML validation
# ---------------------------------------------------------------------------


def test_access_apply_pipeline_yaml_loads() -> None:
    """access_apply_pipeline.yaml loads and validates against schema."""
    from pathlib import Path

    from src.platform.orchestrator.loader import PipelineDefinitionLoader

    # Action-ref validation is skipped to avoid registry-isolation interference
    # from other test modules that call ACTION_REGISTRY._clear_for_tests().
    pipelines_dir = Path(__file__).parents[4] / 'pipelines'
    loader = PipelineDefinitionLoader(validate_action_refs=False)
    pipelines = loader.load_dir(pipelines_dir)

    assert 'access_apply_pipeline' in pipelines
    defn = pipelines['access_apply_pipeline']
    assert defn.version == 1
    # One step: execute_plan
    assert len(defn.steps) == 1
    assert defn.steps[0]['action'] == 'execute_plan'


def test_initiatives_scheduled_replan_scan_yaml_loads() -> None:
    """initiatives_scheduled_replan_scan.yaml loads and validates against schema."""
    from pathlib import Path

    from src.platform.orchestrator.loader import PipelineDefinitionLoader

    pipelines_dir = Path(__file__).parents[4] / 'pipelines'
    loader = PipelineDefinitionLoader(validate_action_refs=False)
    pipelines = loader.load_dir(pipelines_dir)

    assert 'initiatives_scheduled_replan_scan' in pipelines
    defn = pipelines['initiatives_scheduled_replan_scan']
    assert defn.version == 1
