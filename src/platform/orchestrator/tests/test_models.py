# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ORM round-trip tests for platform/orchestrator models.

Tests verify:
- Default column values (status, args, NULLable fields)
- Partial UNIQUE constraint on pipeline_runs (in-flight idempotency)
- Terminal status releases the idempotency slot
- Retry rows bypass the partial UNIQUE
- StepRun composite unique (run_id, step_name, attempt)
- PipelineEventWaiter unique step_run_id
- CASCADE on PipelineRun delete

All tests use the shared ``session_factory`` fixture from src/conftest.py.
Each test commits data explicitly so it is visible across session boundaries.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import src.platform.orchestrator.models  # noqa: F401 — registers models for create_all
from src.platform.orchestrator.models import (
    PipelineEventWaiter,
    PipelineEventWaiterStatus,
    PipelineRun,
    PipelineRunStatus,
    PipelineTriggerSource,
    StepRun,
    StepRunStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(
    *,
    pipeline_name: str = 'test_pipeline',
    pipeline_version: int = 1,
    content_hash: str = 'a' * 64,
    status: PipelineRunStatus = PipelineRunStatus.pending,
    retry_of_run_id: uuid.UUID | None = None,
    trigger_source: PipelineTriggerSource = PipelineTriggerSource.http,
) -> PipelineRun:
    return PipelineRun(
        pipeline_name=pipeline_name,
        pipeline_version=pipeline_version,
        args={},
        content_hash=content_hash,
        status=status,
        retry_of_run_id=retry_of_run_id,
        trigger_source=trigger_source,
    )


def _step(
    *,
    pipeline_run_id: uuid.UUID,
    step_name: str = 'reconcile',
    attempt: int = 1,
    status: StepRunStatus = StepRunStatus.pending,
) -> StepRun:
    return StepRun(
        pipeline_run_id=pipeline_run_id,
        step_name=step_name,
        attempt=attempt,
        args={},
        status=status,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_run_minimum_fields_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Insert with only required fields; verify server-side defaults."""
    run_id = uuid.uuid4()
    row = PipelineRun(
        id=run_id,
        pipeline_name='onboard',
        pipeline_version=1,
        args={},
        content_hash='b' * 64,
        status=PipelineRunStatus.pending,
        trigger_source=PipelineTriggerSource.http,
    )
    async with session_factory() as session:
        session.add(row)
        await session.flush()

        refreshed = await session.get(PipelineRun, run_id)
        assert refreshed is not None
        assert refreshed.args == {}
        assert refreshed.current_step is None
        assert refreshed.retry_of_run_id is None
        assert refreshed.worker_id is None
        assert refreshed.created_at is not None
        assert refreshed.status == PipelineRunStatus.pending


@pytest.mark.asyncio
async def test_pipeline_run_partial_unique_blocks_concurrent_inflight(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two pending rows with the same (name, version, hash) and retry=NULL must conflict."""
    h = 'c' * 64
    run_a_id: uuid.UUID

    # Insert and commit row A.
    async with session_factory() as session:
        row_a = _run(pipeline_name='dup_test', content_hash=h)
        session.add(row_a)
        await session.commit()
        run_a_id = row_a.id

    # Attempt to insert duplicate row B — must raise IntegrityError.
    with pytest.raises(IntegrityError) as exc_info:
        async with session_factory() as session:
            row_b = _run(pipeline_name='dup_test', content_hash=h)
            session.add(row_b)
            await session.commit()

    assert 'uq_pipeline_runs_inflight_idempotency' in str(exc_info.value.orig)

    # Cleanup.
    async with session_factory() as session:
        obj = await session.get(PipelineRun, run_a_id)
        if obj:
            await session.delete(obj)
            await session.commit()


@pytest.mark.asyncio
async def test_pipeline_run_terminal_status_releases_idempotency_slot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Completing a run releases the partial UNIQUE slot; a new run with same triple succeeds."""
    h = 'd' * 64
    run_a_id: uuid.UUID
    run_c_id: uuid.UUID

    # Insert row A, then advance to terminal status.
    async with session_factory() as session:
        row_a = _run(pipeline_name='release_test', content_hash=h)
        session.add(row_a)
        await session.flush()
        run_a_id = row_a.id
        await session.execute(
            sa.update(PipelineRun).where(PipelineRun.id == run_a_id).values(status=PipelineRunStatus.completed)
        )
        await session.commit()

    # Now insert row C with the same triple — slot released by terminal status.
    async with session_factory() as session:
        row_c = _run(pipeline_name='release_test', content_hash=h)
        session.add(row_c)
        await session.commit()  # must not raise
        run_c_id = row_c.id

    # Cleanup.
    async with session_factory() as session:
        for rid in (run_a_id, run_c_id):
            obj = await session.get(PipelineRun, rid)
            if obj:
                await session.delete(obj)
        await session.commit()


@pytest.mark.asyncio
async def test_pipeline_run_retry_row_bypasses_partial_unique(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """retry_of_run_id IS NOT NULL bypasses the partial UNIQUE; both rows can coexist."""
    h = 'e' * 64
    run_a_id: uuid.UUID
    run_b_id: uuid.UUID

    async with session_factory() as session:
        row_a = _run(pipeline_name='retry_test', content_hash=h)
        session.add(row_a)
        await session.commit()
        run_a_id = row_a.id

    # Retry row with same triple but retry_of_run_id set — must succeed.
    async with session_factory() as session:
        row_b = _run(
            pipeline_name='retry_test',
            content_hash=h,
            retry_of_run_id=run_a_id,
            status=PipelineRunStatus.pending,
        )
        session.add(row_b)
        await session.commit()  # must not raise
        run_b_id = row_b.id

    # Cleanup.
    async with session_factory() as session:
        # Delete retry first (it has FK pointing to row_a).
        obj_b = await session.get(PipelineRun, run_b_id)
        if obj_b:
            await session.delete(obj_b)
        await session.commit()
    async with session_factory() as session:
        obj_a = await session.get(PipelineRun, run_a_id)
        if obj_a:
            await session.delete(obj_a)
        await session.commit()


@pytest.mark.asyncio
async def test_pipeline_run_cancelling_still_blocks_idempotency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """cancelling status is in the in-flight set; a second row with same triple must fail."""
    h = 'f' * 64
    run_a_id: uuid.UUID

    async with session_factory() as session:
        row_a = _run(pipeline_name='cancel_test', content_hash=h, status=PipelineRunStatus.cancelling)
        session.add(row_a)
        await session.commit()
        run_a_id = row_a.id

    with pytest.raises(IntegrityError) as exc_info:
        async with session_factory() as session:
            row_b = _run(pipeline_name='cancel_test', content_hash=h)
            session.add(row_b)
            await session.commit()

    assert 'uq_pipeline_runs_inflight_idempotency' in str(exc_info.value.orig)

    # Cleanup.
    async with session_factory() as session:
        obj = await session.get(PipelineRun, run_a_id)
        if obj:
            await session.delete(obj)
            await session.commit()


@pytest.mark.asyncio
async def test_step_run_unique_run_step_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """(pipeline_run_id, step_name, attempt) must be unique; different attempt is allowed."""
    run_id: uuid.UUID

    async with session_factory() as session:
        pr = _run()
        session.add(pr)
        await session.commit()
        run_id = pr.id

    # First attempt — ok.
    async with session_factory() as session:
        s1 = _step(pipeline_run_id=run_id, step_name='reconcile', attempt=1)
        session.add(s1)
        await session.commit()

    # Duplicate attempt — must fail.
    with pytest.raises(IntegrityError) as exc_info:
        async with session_factory() as session:
            s2 = _step(pipeline_run_id=run_id, step_name='reconcile', attempt=1)
            session.add(s2)
            await session.commit()

    assert 'uq_step_runs_run_step_attempt' in str(exc_info.value.orig)

    # Different attempt number — ok.
    async with session_factory() as session:
        s3 = _step(pipeline_run_id=run_id, step_name='reconcile', attempt=2)
        session.add(s3)
        await session.commit()  # must not raise

    # Cleanup (pipeline_run delete cascades to step_runs).
    async with session_factory() as session:
        pr_obj = await session.get(PipelineRun, run_id)
        if pr_obj:
            await session.delete(pr_obj)
            await session.commit()


@pytest.mark.asyncio
async def test_pipeline_event_waiter_unique_step_run_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only one event waiter per step_run_id is allowed; after deletion a new one succeeds."""
    run_id: uuid.UUID
    step_run_id: uuid.UUID

    async with session_factory() as session:
        pr = _run()
        session.add(pr)
        await session.commit()
        run_id = pr.id

    async with session_factory() as session:
        sr = _step(pipeline_run_id=run_id)
        session.add(sr)
        await session.commit()
        step_run_id = sr.id

    expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=10)
    w1_id: uuid.UUID

    # First waiter — ok.
    async with session_factory() as session:
        w1 = PipelineEventWaiter(
            step_run_id=step_run_id,
            event_type='inventory.access_fact.created',
            match={},
            expires_at=expires,
            status=PipelineEventWaiterStatus.waiting,
        )
        session.add(w1)
        await session.commit()
        w1_id = w1.id

    # Duplicate step_run_id — must fail.
    with pytest.raises(IntegrityError) as exc_info:
        async with session_factory() as session:
            w2 = PipelineEventWaiter(
                step_run_id=step_run_id,
                event_type='inventory.access_fact.created',
                match={},
                expires_at=expires,
                status=PipelineEventWaiterStatus.waiting,
            )
            session.add(w2)
            await session.commit()

    assert 'uq_pipeline_event_waiters_step_run_id' in str(exc_info.value.orig)

    # Delete first waiter; new one must succeed.
    async with session_factory() as session:
        existing = await session.get(PipelineEventWaiter, w1_id)
        if existing:
            await session.delete(existing)
            await session.commit()

    async with session_factory() as session:
        w3 = PipelineEventWaiter(
            step_run_id=step_run_id,
            event_type='inventory.access_fact.created',
            match={},
            expires_at=expires,
            status=PipelineEventWaiterStatus.waiting,
        )
        session.add(w3)
        await session.commit()  # must not raise

    # Cleanup.
    async with session_factory() as session:
        pr_obj = await session.get(PipelineRun, run_id)
        if pr_obj:
            await session.delete(pr_obj)
            await session.commit()


@pytest.mark.asyncio
async def test_step_run_cascade_on_pipeline_run_delete(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deleting a PipelineRun cascades to its StepRun children."""
    run_id: uuid.UUID
    step_id: uuid.UUID

    async with session_factory() as session:
        pr = _run()
        session.add(pr)
        await session.commit()
        run_id = pr.id

    async with session_factory() as session:
        sr = _step(pipeline_run_id=run_id)
        session.add(sr)
        await session.commit()
        step_id = sr.id

    # Delete the run; step should cascade.
    async with session_factory() as session:
        pr_obj = await session.get(PipelineRun, run_id)
        assert pr_obj is not None
        await session.delete(pr_obj)
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(sa.select(StepRun).where(StepRun.id == step_id))
        assert result.scalar_one_or_none() is None, 'StepRun should have been CASCADE-deleted'
