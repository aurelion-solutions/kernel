# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Route tests for POST /pipeline-runs/{run_id}/cancel (Step 18)."""

from __future__ import annotations

import hashlib
import json
import uuid

from fastapi import FastAPI
from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.orchestrator.models import PipelineRun, PipelineRunStatus, PipelineTriggerSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_run(
    app: FastAPI,
    *,
    status: PipelineRunStatus = PipelineRunStatus.pending,
    pipeline_name: str = 'cancel_route_test',
) -> PipelineRun:
    """Insert a PipelineRun directly via the app's DB override."""
    args: dict = {}
    content_hash = hashlib.sha256(
        json.dumps(args, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    ).hexdigest()
    # append status to make it unique so parallel tests don't collide
    content_hash = hashlib.sha256((content_hash + status.value + str(uuid.uuid4())).encode()).hexdigest()

    override = app.dependency_overrides.get(__import__('src.core.db.deps', fromlist=['get_db']).get_db)
    gen = override()
    session: AsyncSession = await gen.__anext__()
    try:
        run = PipelineRun(
            pipeline_name=pipeline_name,
            pipeline_version=1,
            args=args,
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_pending_run_returns_200_cancelled(app: FastAPI, client: AsyncClient) -> None:
    """POST cancel on a pending run → 200 with status='cancelled'."""
    run = await _insert_run(app, status=PipelineRunStatus.pending)
    resp = await client.post(f'/api/v0/pipeline-runs/{run.id}/cancel')
    assert resp.status_code == 200
    body = resp.json()
    assert body['run_id'] == str(run.id)
    assert body['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_cancel_running_run_returns_200_cancelling(app: FastAPI, client: AsyncClient) -> None:
    """POST cancel on a running run → 200 with status='cancelling'."""
    run = await _insert_run(app, status=PipelineRunStatus.running)
    resp = await client.post(f'/api/v0/pipeline-runs/{run.id}/cancel')
    assert resp.status_code == 200
    body = resp.json()
    assert body['status'] == 'cancelling'


@pytest.mark.asyncio
async def test_cancel_unknown_run_returns_404(app: FastAPI, client: AsyncClient) -> None:
    """POST cancel on unknown run_id → 404."""
    unknown = uuid.uuid4()
    resp = await client.post(f'/api/v0/pipeline-runs/{unknown}/cancel')
    assert resp.status_code == 404
    assert 'Pipeline run not found' in resp.json()['detail']


@pytest.mark.asyncio
async def test_cancel_already_cancelling_returns_409(app: FastAPI, client: AsyncClient) -> None:
    """POST cancel on a cancelling run → 409."""
    run = await _insert_run(app, status=PipelineRunStatus.cancelling)
    resp = await client.post(f'/api/v0/pipeline-runs/{run.id}/cancel')
    assert resp.status_code == 409
    assert 'already cancelling' in resp.json()['detail']


@pytest.mark.asyncio
async def test_cancel_terminal_run_returns_409(app: FastAPI, client: AsyncClient) -> None:
    """POST cancel on a completed run → 409 with 'terminal' in detail."""
    run = await _insert_run(app, status=PipelineRunStatus.completed)
    resp = await client.post(f'/api/v0/pipeline-runs/{run.id}/cancel')
    assert resp.status_code == 409
    assert 'terminal' in resp.json()['detail']
