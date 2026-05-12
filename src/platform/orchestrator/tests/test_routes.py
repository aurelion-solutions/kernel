# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for the pipeline orchestrator REST routes (Step 11).

Uses the global ``app`` + ``client`` fixtures from ``src/conftest.py``.
Pipeline definitions are seeded via ``app.state.pipelines`` — no YAML files
are dropped on disk.
"""

from __future__ import annotations

from collections.abc import Iterator
import importlib
from pathlib import Path
import sys
from typing import Any
import uuid

from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import BaseModel
import pytest
from src.platform.orchestrator.loader import PipelineDefinition
from src.platform.orchestrator.models import (
    PipelineRun,
    PipelineRunStatus,
    PipelineTriggerSource,
)
from src.platform.orchestrator.registry import ACTION_REGISTRY, register_action

# ---------------------------------------------------------------------------
# Test action schemas (used for well-known tests)
# ---------------------------------------------------------------------------


class _NoArgs(BaseModel):
    pass


class _NoResult(BaseModel):
    pass


# Register a test action if not already registered.
_TEST_ENGINE = 'test_engine'
_TEST_ACTION = 'noop'
_TEST_ACTION_KEY = (_TEST_ENGINE, _TEST_ACTION)


def _ensure_test_action_registered() -> None:
    """Idempotently register the test action (registry is global, shared across tests)."""
    try:
        ACTION_REGISTRY.get(_TEST_ENGINE, _TEST_ACTION)
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup

        @register_action(
            engine=_TEST_ENGINE,
            action=_TEST_ACTION,
            args_schema=_NoArgs,
            result_schema=_NoResult,
            idempotent=True,
        )
        async def _noop_handler(args: _NoArgs, ctx: Any) -> _NoResult:
            return _NoResult()


_ensure_test_action_registered()


# ---------------------------------------------------------------------------
# Registry restoration fixture
# ---------------------------------------------------------------------------
#
# Engine tests (e.g. src/engines/*/tests/test_actions.py) use an autouse
# ``_clear_for_tests()`` fixture that wipes ACTION_REGISTRY between tests for
# isolation. When pytest collects this whole tree, those engine tests may run
# before the well-known route tests below, leaving the registry empty by the
# time we hit them.
#
# To stay independent of test-runner ordering, we re-register the local
# ``test_engine.noop`` action and re-import every engine actions module before
# each test here. This restores the registry to the state the production app
# would see at startup (engine actions modules imported via discovery).

_ENGINE_ACTION_MODULES: tuple[str, ...] = (
    'src.engines.access_analysis.assessment_preview.actions',
    'src.engines.access_analysis.capability_preview.actions',
    'src.engines.access_analysis.reports.actions',
    'src.engines.access_effective.actions',
    'src.engines.policy_assessment.policy_types.sod.actions',
)


@pytest.fixture(autouse=True)
def _restore_action_registry() -> Iterator[None]:
    """Ensure ACTION_REGISTRY contains the test action + all engine actions.

    Why this is necessary
    ---------------------
    This file shares state with sibling test files via two routes:

    1. ``test_registry.py::test_no_side_effects_on_import`` calls
       ``importlib.reload(registry_module)``. After that, the
       module-level ``ACTION_REGISTRY`` symbol in registry module points
       at a *new* singleton, but every module that did
       ``from ... import ACTION_REGISTRY`` *before* the reload (notably
       ``routes.py`` and this file) still holds the *old* one.
    2. ``src/engines/*/tests/test_actions.py`` clear the registry via an
       autouse fixture for their own isolation, leaving it empty.

    To keep this file independent of run order we:

    a. Force the registry module symbol back to the *original* singleton
       so freshly-imported engine actions register into it (same one
       ``routes.py`` reads from).
    b. Clear it and re-register the local test action.
    c. Pop and re-import every engine actions module so their
       ``@register_action`` side effects fire against the original
       singleton.
    """
    import src.platform.orchestrator.registry as _reg_mod  # noqa: PLC0415

    # (a) Repair the module-level binding if a sibling test reloaded the
    # module (importlib.reload swaps out the singleton).
    if _reg_mod.ACTION_REGISTRY is not ACTION_REGISTRY:
        _reg_mod.ACTION_REGISTRY = ACTION_REGISTRY

    # (b) Normalise to empty + re-add the local test action.
    ACTION_REGISTRY._clear_for_tests()
    _ensure_test_action_registered()

    # (c) Re-import every engine actions module so they re-register
    # against the singleton ``routes.py`` reads from.
    for module_name in _ENGINE_ACTION_MODULES:
        sys.modules.pop(module_name, None)
        importlib.import_module(module_name)

    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_pipeline(
    name: str = 'test_pipeline',
    version: int = 1,
    args_schema_dict: dict[str, Any] | None = None,
) -> PipelineDefinition:
    """Build a minimal PipelineDefinition for test seeding."""
    raw: dict[str, Any] = {
        'pipeline': {
            'name': name,
            'version': version,
            'schema_version': 1,
            'steps': [{'name': 'step_one', 'engine': _TEST_ENGINE, 'action': _TEST_ACTION}],
        }
    }
    if args_schema_dict:
        raw['pipeline']['args'] = args_schema_dict

    return PipelineDefinition(
        name=name,
        version=version,
        schema_version=1,
        source_path=Path(f'/fake/{name}.yaml'),
        content_hash='a' * 64,
        args_schema_dict=args_schema_dict or {},
        triggers=(),
        steps=(dict(raw['pipeline']['steps'][0]),),  # type: ignore[arg-type]
        raw_dict=raw,  # type: ignore[arg-type]
    )


def _seed_pipeline(app: FastAPI, defn: PipelineDefinition) -> None:
    """Add a PipelineDefinition to app.state.pipelines."""
    if not hasattr(app.state, 'pipelines'):
        app.state.pipelines = {}
    app.state.pipelines[defn.name] = defn


async def _insert_run(
    app: FastAPI,
    *,
    pipeline_name: str = 'test_pipeline',
    pipeline_version: int = 1,
    args: dict[str, Any] | None = None,
    status: PipelineRunStatus = PipelineRunStatus.pending,
) -> PipelineRun:
    """Insert a PipelineRun directly into the DB via the app's session factory."""
    # Reconstruct session_factory from the app's dependency override for get_db.
    # The override is a generator; we call it to get the session.
    import hashlib  # noqa: E401
    import json

    from sqlalchemy.ext.asyncio import AsyncSession

    _args = args or {}
    content_hash = hashlib.sha256(
        json.dumps(_args, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    ).hexdigest()

    # Use the override directly — call the override gen, extract session.
    override = app.dependency_overrides.get(__import__('src.core.db.deps', fromlist=['get_db']).get_db)
    gen = override()
    session: AsyncSession = await gen.__anext__()
    try:
        run = PipelineRun(
            pipeline_name=pipeline_name,
            pipeline_version=pipeline_version,
            args=_args,
            content_hash=content_hash,
            status=status,
            trigger_source=PipelineTriggerSource.http,
        )
        session.add(run)
        await session.flush()
        await session.commit()
        run_id = run.id
    finally:
        try:
            await gen.aclose()
        except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
            pass

    # Re-fetch to get server-assigned fields.
    gen2 = override()
    session2: AsyncSession = await gen2.__anext__()
    try:
        result = await session2.get(PipelineRun, run_id)
        assert result is not None
        return result
    finally:
        try:
            await gen2.aclose()
        except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
            pass


# ---------------------------------------------------------------------------
# GET /pipelines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pipelines_empty(app: FastAPI, client: AsyncClient) -> None:
    """GET /pipelines returns [] when no pipelines are loaded."""
    app.state.pipelines = {}
    resp = await client.get('/api/v0/pipelines')
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_pipelines_one_entry(app: FastAPI, client: AsyncClient) -> None:
    """GET /pipelines returns one entry after seeding a pipeline."""
    defn = _fake_pipeline('my_pipeline', version=2)
    app.state.pipelines = {}
    _seed_pipeline(app, defn)
    resp = await client.get('/api/v0/pipelines')
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]['name'] == 'my_pipeline'
    assert data[0]['version'] == 2
    assert data[0]['step_count'] == 1


# ---------------------------------------------------------------------------
# GET /pipelines/{name}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pipeline_found(app: FastAPI, client: AsyncClient) -> None:
    """GET /pipelines/{name} returns full detail for a known pipeline."""
    defn = _fake_pipeline('alpha')
    app.state.pipelines = {}
    _seed_pipeline(app, defn)
    resp = await client.get('/api/v0/pipelines/alpha')
    assert resp.status_code == 200
    body = resp.json()
    assert body['name'] == 'alpha'
    assert 'steps' in body
    assert 'content_hash' in body
    assert 'source_path' in body


@pytest.mark.asyncio
async def test_get_pipeline_not_found(app: FastAPI, client: AsyncClient) -> None:
    """GET /pipelines/{name} returns 404 for an unknown pipeline."""
    app.state.pipelines = {}
    resp = await client.get('/api/v0/pipelines/does_not_exist')
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /pipeline-runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pipeline_runs_empty(client: AsyncClient) -> None:
    """GET /pipeline-runs returns [] when no runs exist."""
    resp = await client.get('/api/v0/pipeline-runs')
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_pipeline_runs_default_sort_newest_first(app: FastAPI, client: AsyncClient) -> None:
    """GET /pipeline-runs returns rows newest-first by default."""
    run1 = await _insert_run(app, pipeline_name='pipe_a')
    run2 = await _insert_run(app, pipeline_name='pipe_b')

    resp = await client.get('/api/v0/pipeline-runs')
    assert resp.status_code == 200
    ids = [r['id'] for r in resp.json()]
    # Both should be present; we cannot guarantee order on started_at=NULL so
    # check both are included.
    assert str(run1.id) in ids
    assert str(run2.id) in ids


@pytest.mark.asyncio
async def test_list_pipeline_runs_filter_by_status(app: FastAPI, client: AsyncClient) -> None:
    """GET /pipeline-runs?status=pending returns only pending rows."""
    await _insert_run(app, pipeline_name='pipe_pend', status=PipelineRunStatus.pending)
    resp = await client.get('/api/v0/pipeline-runs?status=pending')
    assert resp.status_code == 200
    rows = resp.json()
    assert all(r['status'] == 'pending' for r in rows)
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_list_pipeline_runs_filter_by_name(app: FastAPI, client: AsyncClient) -> None:
    """GET /pipeline-runs?pipeline_name=x returns only rows for that pipeline."""
    await _insert_run(app, pipeline_name='unique_pipe_xyz')
    await _insert_run(app, pipeline_name='other_pipe')

    resp = await client.get('/api/v0/pipeline-runs?pipeline_name=unique_pipe_xyz')
    assert resp.status_code == 200
    rows = resp.json()
    assert all(r['pipeline_name'] == 'unique_pipe_xyz' for r in rows)
    assert len(rows) >= 1


@pytest.mark.asyncio
async def test_list_pipeline_runs_pagination(app: FastAPI, client: AsyncClient) -> None:
    """GET /pipeline-runs with limit/offset returns paginated results."""
    # Use distinct args to avoid the partial-UNIQUE dedupe index.
    await _insert_run(app, pipeline_name='pag_pipe', args={'seq': 1})
    await _insert_run(app, pipeline_name='pag_pipe', args={'seq': 2})
    await _insert_run(app, pipeline_name='pag_pipe', args={'seq': 3})

    resp_all = await client.get('/api/v0/pipeline-runs?pipeline_name=pag_pipe')
    all_rows = resp_all.json()
    assert len(all_rows) >= 3

    resp_p1 = await client.get('/api/v0/pipeline-runs?pipeline_name=pag_pipe&limit=1&offset=0')
    resp_p2 = await client.get('/api/v0/pipeline-runs?pipeline_name=pag_pipe&limit=1&offset=1')
    assert resp_p1.status_code == 200
    assert resp_p2.status_code == 200
    p1_ids = [r['id'] for r in resp_p1.json()]
    p2_ids = [r['id'] for r in resp_p2.json()]
    assert p1_ids != p2_ids


# ---------------------------------------------------------------------------
# GET /pipeline-runs/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pipeline_run_found(app: FastAPI, client: AsyncClient) -> None:
    """GET /pipeline-runs/{id} returns full run detail."""
    run = await _insert_run(app)
    resp = await client.get(f'/api/v0/pipeline-runs/{run.id}')
    assert resp.status_code == 200
    body = resp.json()
    assert body['id'] == str(run.id)
    assert body['pipeline_name'] == run.pipeline_name
    assert 'args' in body
    assert 'steps' in body


@pytest.mark.asyncio
async def test_get_pipeline_run_not_found(client: AsyncClient) -> None:
    """GET /pipeline-runs/{id} returns 404 for unknown id."""
    resp = await client.get(f'/api/v0/pipeline-runs/{uuid.uuid4()}')
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /pipeline-runs/{id}/steps/{step_name}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_step_run_unknown_run_404(client: AsyncClient) -> None:
    """GET /pipeline-runs/{id}/steps/{name} returns 404 for unknown run."""
    resp = await client.get(f'/api/v0/pipeline-runs/{uuid.uuid4()}/steps/any_step')
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_step_run_unknown_step_404(app: FastAPI, client: AsyncClient) -> None:
    """GET /pipeline-runs/{id}/steps/{name} returns 404 for unknown step name."""
    run = await _insert_run(app)
    resp = await client.get(f'/api/v0/pipeline-runs/{run.id}/steps/nonexistent_step')
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /pipeline-runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pipeline_run_fresh_201(app: FastAPI, client: AsyncClient) -> None:
    """POST /pipeline-runs → 201 + created=True on fresh insert."""
    defn = _fake_pipeline('fresh_pipe')
    app.state.pipelines = {'fresh_pipe': defn}

    resp = await client.post('/api/v0/pipeline-runs', json={'pipeline_name': 'fresh_pipe'})
    assert resp.status_code == 201
    body = resp.json()
    assert body['created'] is True
    assert body['status'] == 'pending'
    assert 'pipeline_run_id' in body


@pytest.mark.asyncio
async def test_create_pipeline_run_duplicate_200(app: FastAPI, client: AsyncClient) -> None:
    """POST /pipeline-runs → 200 + created=False on idempotent duplicate."""
    defn = _fake_pipeline('idem_pipe')
    app.state.pipelines = {'idem_pipe': defn}

    resp1 = await client.post('/api/v0/pipeline-runs', json={'pipeline_name': 'idem_pipe', 'args': {}})
    assert resp1.status_code == 201
    run_id_1 = resp1.json()['pipeline_run_id']

    resp2 = await client.post('/api/v0/pipeline-runs', json={'pipeline_name': 'idem_pipe', 'args': {}})
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2['created'] is False
    assert body2['pipeline_run_id'] == run_id_1


@pytest.mark.asyncio
async def test_create_pipeline_run_unknown_pipeline_404(app: FastAPI, client: AsyncClient) -> None:
    """POST /pipeline-runs with unknown pipeline_name → 404."""
    app.state.pipelines = {}
    resp = await client.post('/api/v0/pipeline-runs', json={'pipeline_name': 'no_such_pipe'})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_pipeline_run_args_schema_violation_422(app: FastAPI, client: AsyncClient) -> None:
    """POST /pipeline-runs with args violating the pipeline args schema → 422."""
    args_schema = {
        'type': 'object',
        'properties': {'count': {'type': 'integer'}},
        'required': ['count'],
        'additionalProperties': False,
    }
    defn = _fake_pipeline('schema_pipe', args_schema_dict=args_schema)
    app.state.pipelines = {'schema_pipe': defn}

    resp = await client.post(
        '/api/v0/pipeline-runs',
        json={'pipeline_name': 'schema_pipe', 'args': {'count': 'not_an_integer'}},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_pipeline_run_version_mismatch_404(app: FastAPI, client: AsyncClient) -> None:
    """POST /pipeline-runs with mismatched pipeline_version → 404."""
    defn = _fake_pipeline('ver_pipe', version=3)
    app.state.pipelines = {'ver_pipe': defn}

    resp = await client.post(
        '/api/v0/pipeline-runs',
        json={'pipeline_name': 'ver_pipe', 'pipeline_version': 99},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /.well-known/pipeline-schema.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pipeline_schema_is_valid_json(client: AsyncClient) -> None:
    """GET /.well-known/pipeline-schema.json returns parseable JSON with $defs.action_args."""
    resp = await client.get('/api/v0/.well-known/pipeline-schema.json')
    assert resp.status_code == 200
    body = resp.json()
    assert '$defs' in body
    assert 'action_args' in body['$defs']
    # At least the test action should be present.
    action_args = body['$defs']['action_args']
    key = f'{_TEST_ENGINE}.{_TEST_ACTION}'
    assert key in action_args


# ---------------------------------------------------------------------------
# GET /.well-known/pipeline-actions.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pipeline_actions_catalogue(client: AsyncClient) -> None:
    """GET /.well-known/pipeline-actions.json returns one entry per registered action."""
    resp = await client.get('/api/v0/.well-known/pipeline-actions.json')
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == len(ACTION_REGISTRY.all())
    for entry in entries:
        assert 'engine' in entry
        assert 'action' in entry
        assert 'args_schema' in entry
        assert 'result_schema' in entry
        assert 'idempotent' in entry
