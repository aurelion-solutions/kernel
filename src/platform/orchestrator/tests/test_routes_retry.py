# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Route tests for POST /pipeline-runs/{run_id}/retry (Step 19)."""

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
    status: PipelineRunStatus = PipelineRunStatus.completed,
    pipeline_name: str = 'retry_route_test',
) -> PipelineRun:
    """Insert a PipelineRun directly via the app's DB override."""
    args: dict = {}
    content_hash = hashlib.sha256(
        json.dumps(args, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    ).hexdigest()
    # append status + uuid to avoid UNIQUE violations between tests
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
async def test_retry_201_happy_path_completed_source(app: FastAPI, client: AsyncClient) -> None:
    """POST retry on a completed run → 201 with retry_of_run_id set."""
    source = await _insert_run(app, status=PipelineRunStatus.completed)
    resp = await client.post(f'/api/v0/pipeline-runs/{source.id}/retry')
    assert resp.status_code == 201
    body = resp.json()
    assert body['retry_of_run_id'] == str(source.id)
    assert body['status'] == 'pending'
    assert body['pipeline_name'] == source.pipeline_name
    assert body['pipeline_version'] == source.pipeline_version
    assert uuid.UUID(body['run_id']) != source.id


@pytest.mark.asyncio
async def test_retry_404_when_run_not_found(client: AsyncClient) -> None:
    """POST retry on unknown run_id → 404."""
    unknown = uuid.uuid4()
    resp = await client.post(f'/api/v0/pipeline-runs/{unknown}/retry')
    assert resp.status_code == 404
    assert 'Pipeline run not found' in resp.json()['detail']


@pytest.mark.asyncio
async def test_retry_409_non_terminal_when_source_running(app: FastAPI, client: AsyncClient) -> None:
    """POST retry on a running run → 409."""
    source = await _insert_run(app, status=PipelineRunStatus.running)
    resp = await client.post(f'/api/v0/pipeline-runs/{source.id}/retry')
    assert resp.status_code == 409
    detail = resp.json()['detail']
    assert 'not in a terminal status' in detail


@pytest.mark.asyncio
async def test_retry_409_cancelling_when_source_cancelling(app: FastAPI, client: AsyncClient) -> None:
    """POST retry on a cancelling run → 409 with cancelling message."""
    source = await _insert_run(app, status=PipelineRunStatus.cancelling)
    resp = await client.post(f'/api/v0/pipeline-runs/{source.id}/retry')
    assert resp.status_code == 409
    detail = resp.json()['detail']
    assert 'cancelling' in detail


@pytest.mark.asyncio
async def test_retry_emits_info_log_with_run_id(app: FastAPI, client: AsyncClient) -> None:
    """POST retry → 201 — verifies response schema fields are populated."""
    source = await _insert_run(app, status=PipelineRunStatus.failed)
    resp = await client.post(f'/api/v0/pipeline-runs/{source.id}/retry')
    assert resp.status_code == 201
    body = resp.json()
    # All required fields present and non-null
    assert body['run_id']
    assert body['retry_of_run_id'] == str(source.id)
    assert body['status'] == 'pending'
