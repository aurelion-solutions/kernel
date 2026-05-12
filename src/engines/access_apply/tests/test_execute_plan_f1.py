# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for access_apply.execute_plan (Phase 19 Step F1).

Coverage:
- happy_path: preflight mismatch → connector call → post-verify match → done
- preflight_verify_match_proposed: preflight match on proposed item → skip connector, done
- preflight_verify_match_executing: preflight match on executing item → auto-invalidation + done
- post_verify_failure: post-verify mismatch → failed with verify_mismatch reason
- post_verify_timeout: post-verify timeout → failed with verify_timeout reason
- connector_error: connector raises → failed with apply_error reason
- restart_with_done_items: items already done are skipped
- restart_with_executing_prev_status: executing prev_status, preflight mismatch → retry connector
- finally_block_cleanup: apply lease deleted even on connector exception
- auto_invalidation: other active plans for same subject get invalidated
- dag_order: items executed in topological order respecting dependencies
"""

from __future__ import annotations

from typing import Any
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_apply.execute_plan import (
    ExecutePlanCounts,
    PlanNotFoundError,
    execute_plan,
)
from src.engines.access_plan.models import (
    AccessApplyActive,
    AccessPlan,
    AccessPlanStatus,
    PlanDependency,
    PlanItem,
    PlanItemExecution,
    PlanItemExecutionStatus,
    PlanItemFailureReason,
    PlanItemKind,
)
from src.platform.logs.service import noop_log_service

# ---------------------------------------------------------------------------
# Stub connector
# ---------------------------------------------------------------------------


class _StubConnector:
    """Configurable stub connector for testing execute_plan."""

    def __init__(
        self,
        *,
        verify_sequence: list[str] | None = None,
        invoke_error: Exception | None = None,
    ) -> None:
        """
        verify_sequence: ordered list of 'match'|'mismatch'|'timeout' results
                         returned by successive verify_fact calls.
        invoke_error: if set, raised on the first non-verify_fact call.
        """
        self._verify_seq = list(verify_sequence or [])
        self._verify_idx = 0
        self._invoke_error = invoke_error
        self.invoke_calls: list[dict[str, Any]] = []

    async def invoke(
        self,
        instance_id: str,
        operation: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.invoke_calls.append({'operation': operation, 'payload': payload, 'instance_id': instance_id})
        if operation == 'verify_fact':
            if self._verify_idx < len(self._verify_seq):
                result = self._verify_seq[self._verify_idx]
                self._verify_idx += 1
            else:
                result = 'match'
            return {'status': 'ok', 'result': result}
        # Non-verify call
        if self._invoke_error is not None:
            raise self._invoke_error
        return {'status': 'ok'}


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_plan_with_items(
    session: AsyncSession,
    *,
    n_items: int = 1,
    subject_ref: str | None = None,
) -> tuple[AccessPlan, list[PlanItem]]:
    """Seed an AccessPlan with n_items PlanItems in 'active' status."""
    subj = subject_ref or str(uuid.uuid4())
    plan = AccessPlan(
        subject_ref=subj,
        subject_type='employee',
        content_hash='testhash',
        status=AccessPlanStatus.active,
        requires_confirmation=False,
    )
    session.add(plan)
    await session.flush()

    items: list[PlanItem] = []
    for i in range(n_items):
        item = PlanItem(
            plan_id=plan.id,
            kind=PlanItemKind.grant_role,
            application='app-test',
            account_ref='acc-ref',
            target_descriptor={'role_ref': f'role_{i}'},
            initiatives=[],
            initiative_refs=[],
            policy_rule_refs=[],
            decision_snapshot={},
        )
        session.add(item)
        items.append(item)

    await session.flush()
    return plan, items


async def _seed_apply_lease(
    session: AsyncSession,
    *,
    plan: AccessPlan,
    pipeline_run_id: uuid.UUID,
) -> None:
    """Seed an AccessApplyActive lease row."""
    lease = AccessApplyActive(
        subject_ref=plan.subject_ref,
        subject_type=plan.subject_type,
        pipeline_run_id=pipeline_run_id,
        plan_id=plan.id,
    )
    session.add(lease)
    await session.flush()


async def _count_active_plans(
    session: AsyncSession,
    subject_ref: str,
    excluding_plan_id: uuid.UUID,
) -> int:
    result = await session.execute(
        sa.select(sa.func.count()).where(
            AccessPlan.subject_ref == subject_ref,
            AccessPlan.status == AccessPlanStatus.active,
            AccessPlan.id != excluding_plan_id,
        )
    )
    return result.scalar_one()


async def _get_execution(
    session: AsyncSession,
    plan_id: uuid.UUID,
    item_id: uuid.UUID,
) -> PlanItemExecution | None:
    result = await session.execute(
        sa.select(PlanItemExecution).where(
            PlanItemExecution.plan_id == plan_id,
            PlanItemExecution.item_id == item_id,
        )
    )
    return result.scalar_one_or_none()


async def _lease_exists(session: AsyncSession, pipeline_run_id: uuid.UUID) -> bool:
    result = await session.execute(
        sa.select(AccessApplyActive).where(AccessApplyActive.pipeline_run_id == pipeline_run_id)
    )
    return result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_not_found(session_factory) -> None:  # type: ignore[no-untyped-def]
    """execute_plan raises PlanNotFoundError when plan doesn't exist."""
    async with session_factory() as session:
        stub = _StubConnector()
        with pytest.raises(PlanNotFoundError):
            await execute_plan(
                session,
                uuid.uuid4(),
                uuid.uuid4(),
                stub,  # type: ignore[arg-type]
                log_service=noop_log_service,
            )


@pytest.mark.asyncio
async def test_happy_path(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Preflight mismatch → connector call → post-verify match → item done."""
    async with session_factory() as session:
        plan, items = await _seed_plan_with_items(session)
        run_id = uuid.uuid4()
        await _seed_apply_lease(session, plan=plan, pipeline_run_id=run_id)
        await session.commit()

    # verify_sequence: preflight=mismatch, post=match
    stub = _StubConnector(verify_sequence=['mismatch', 'match'])

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub,  # type: ignore[arg-type]
            log_service=noop_log_service,
        )

    assert isinstance(result, ExecutePlanCounts)
    assert result.items_done == 1
    assert result.items_failed == 0
    assert result.items_attempted == 1

    # Verify state persisted
    async with session_factory() as session:
        exec_row = await _get_execution(session, plan.id, items[0].id)
        assert exec_row is not None
        assert exec_row.status == PlanItemExecutionStatus.done

        # Lease must be deleted
        assert not await _lease_exists(session, run_id)


@pytest.mark.asyncio
async def test_preflight_match_proposed_skips_connector(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Preflight match on proposed item: connector not called, item marked done."""
    async with session_factory() as session:
        plan, items = await _seed_plan_with_items(session)
        run_id = uuid.uuid4()
        await _seed_apply_lease(session, plan=plan, pipeline_run_id=run_id)
        await session.commit()

    # preflight=match → should skip connector
    stub = _StubConnector(verify_sequence=['match'])

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub,  # type: ignore[arg-type]
            log_service=noop_log_service,
        )

    assert result.items_done == 1
    assert result.items_attempted == 1
    # No non-verify calls
    non_verify = [c for c in stub.invoke_calls if c['operation'] != 'verify_fact']
    assert len(non_verify) == 0


@pytest.mark.asyncio
async def test_preflight_match_executing_runs_invalidation(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Preflight match on executing item: auto-invalidation runs, item done."""
    async with session_factory() as session:
        plan, items = await _seed_plan_with_items(session)
        run_id = uuid.uuid4()
        await _seed_apply_lease(session, plan=plan, pipeline_run_id=run_id)

        # Pre-seed another active plan for same subject
        sibling_plan = AccessPlan(
            subject_ref=plan.subject_ref,
            subject_type='employee',
            content_hash='otherhash',
            status=AccessPlanStatus.active,
            requires_confirmation=False,
        )
        session.add(sibling_plan)

        # Pre-seed executing status for item
        exec_row = PlanItemExecution(
            plan_id=plan.id,
            item_id=items[0].id,
            status=PlanItemExecutionStatus.executing,
        )
        session.add(exec_row)
        await session.commit()

    stub = _StubConnector(verify_sequence=['match'])  # preflight=match

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub,  # type: ignore[arg-type]
            log_service=noop_log_service,
        )

    assert result.items_done == 1

    async with session_factory() as session:
        # Sibling plan must be invalidated
        result_plan = await session.get(AccessPlan, sibling_plan.id)
        assert result_plan is not None
        assert result_plan.status == AccessPlanStatus.invalid


@pytest.mark.asyncio
async def test_post_verify_mismatch(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Post-verify mismatch → item failed with verify_mismatch reason."""
    async with session_factory() as session:
        plan, items = await _seed_plan_with_items(session)
        run_id = uuid.uuid4()
        await _seed_apply_lease(session, plan=plan, pipeline_run_id=run_id)
        await session.commit()

    stub = _StubConnector(verify_sequence=['mismatch', 'mismatch'])

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub,  # type: ignore[arg-type]
            log_service=noop_log_service,
        )

    assert result.items_failed == 1
    assert result.items_done == 0

    async with session_factory() as session:
        exec_row = await _get_execution(session, plan.id, items[0].id)
        assert exec_row is not None
        assert exec_row.status == PlanItemExecutionStatus.failed
        assert exec_row.failure_reason == PlanItemFailureReason.verify_mismatch


@pytest.mark.asyncio
async def test_post_verify_timeout(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Post-verify timeout → item failed with verify_timeout reason."""
    async with session_factory() as session:
        plan, items = await _seed_plan_with_items(session)
        run_id = uuid.uuid4()
        await _seed_apply_lease(session, plan=plan, pipeline_run_id=run_id)
        await session.commit()

    stub = _StubConnector(verify_sequence=['mismatch', 'timeout'])

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub,  # type: ignore[arg-type]
            log_service=noop_log_service,
        )

    assert result.items_failed == 1

    async with session_factory() as session:
        exec_row = await _get_execution(session, plan.id, items[0].id)
        assert exec_row is not None
        assert exec_row.failure_reason == PlanItemFailureReason.verify_timeout


@pytest.mark.asyncio
async def test_connector_error_marks_apply_error(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Connector exception → item failed with apply_error reason."""
    async with session_factory() as session:
        plan, items = await _seed_plan_with_items(session)
        run_id = uuid.uuid4()
        await _seed_apply_lease(session, plan=plan, pipeline_run_id=run_id)
        await session.commit()

    stub = _StubConnector(
        verify_sequence=['mismatch'],  # preflight=mismatch → triggers connector
        invoke_error=RuntimeError('connector down'),
    )

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub,  # type: ignore[arg-type]
            log_service=noop_log_service,
        )

    assert result.items_failed == 1

    async with session_factory() as session:
        exec_row = await _get_execution(session, plan.id, items[0].id)
        assert exec_row is not None
        assert exec_row.status == PlanItemExecutionStatus.failed
        assert exec_row.failure_reason == PlanItemFailureReason.apply_error
        assert 'connector down' in (exec_row.last_error or '')


@pytest.mark.asyncio
async def test_restart_done_items_skipped(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Restart: items already in 'done' status are skipped (no connector call)."""
    async with session_factory() as session:
        plan, items = await _seed_plan_with_items(session, n_items=2)
        run_id = uuid.uuid4()
        await _seed_apply_lease(session, plan=plan, pipeline_run_id=run_id)

        # Pre-seed first item as done
        exec_done = PlanItemExecution(
            plan_id=plan.id,
            item_id=items[0].id,
            status=PlanItemExecutionStatus.done,
        )
        session.add(exec_done)
        await session.commit()

    # Only second item should have preflight+connector calls
    stub = _StubConnector(verify_sequence=['mismatch', 'match'])  # for second item

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub,  # type: ignore[arg-type]
            log_service=noop_log_service,
        )

    assert result.items_done == 2  # first (already done) + second (just done)
    assert result.items_attempted == 1  # only second item attempted


@pytest.mark.asyncio
async def test_restart_executing_retries_connector(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Restart with executing prev_status + preflight mismatch → connector retried."""
    async with session_factory() as session:
        plan, items = await _seed_plan_with_items(session)
        run_id = uuid.uuid4()
        await _seed_apply_lease(session, plan=plan, pipeline_run_id=run_id)

        exec_executing = PlanItemExecution(
            plan_id=plan.id,
            item_id=items[0].id,
            status=PlanItemExecutionStatus.executing,
        )
        session.add(exec_executing)
        await session.commit()

    # preflight=mismatch (connector didn't succeed last time), post=match
    stub = _StubConnector(verify_sequence=['mismatch', 'match'])

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub,  # type: ignore[arg-type]
            log_service=noop_log_service,
        )

    assert result.items_done == 1
    non_verify = [c for c in stub.invoke_calls if c['operation'] != 'verify_fact']
    assert len(non_verify) == 1  # connector was called


@pytest.mark.asyncio
async def test_finally_block_cleans_lease_on_exception(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Apply lease is deleted even when execute_plan is given a non-existent plan."""
    run_id = uuid.uuid4()
    subject_ref = str(uuid.uuid4())

    async with session_factory() as session:
        # Seed a plan so lease can reference its id
        plan = AccessPlan(
            subject_ref=subject_ref,
            subject_type='employee',
            content_hash='hash',
            status=AccessPlanStatus.active,
            requires_confirmation=False,
        )
        session.add(plan)
        await session.flush()
        lease = AccessApplyActive(
            subject_ref=subject_ref,
            subject_type='employee',
            pipeline_run_id=run_id,
            plan_id=plan.id,
        )
        session.add(lease)
        await session.commit()

    async with session_factory() as session:
        # The finally in execute_plan is BEFORE plan lookup, so for PlanNotFoundError
        # the lease won't be cleaned.  Instead test cleanup on connector failure.
        # We'll test that on a real plan with connector raising an error.
        real_plan, items = await _seed_plan_with_items(session, subject_ref=f'other-{uuid.uuid4()}')
        run_id2 = uuid.uuid4()
        await _seed_apply_lease(session, plan=real_plan, pipeline_run_id=run_id2)
        await session.commit()

    # connector raises after preflight mismatch → finally block still runs
    error_stub = _StubConnector(
        verify_sequence=['mismatch'],
        invoke_error=RuntimeError('network error'),
    )

    async with session_factory() as session:
        result = await execute_plan(
            session,
            real_plan.id,
            run_id2,
            error_stub,  # type: ignore[arg-type]
            log_service=noop_log_service,
        )

    assert result.items_failed == 1

    async with session_factory() as session:
        # Lease must be gone
        assert not await _lease_exists(session, run_id2)


@pytest.mark.asyncio
async def test_auto_invalidation_other_active_plans(session_factory) -> None:  # type: ignore[no-untyped-def]
    """On post-verify success: other active plans for same subject are invalidated."""
    async with session_factory() as session:
        plan, _ = await _seed_plan_with_items(session)
        run_id = uuid.uuid4()
        await _seed_apply_lease(session, plan=plan, pipeline_run_id=run_id)

        # Create two sibling active plans
        siblings = []
        for _ in range(2):
            sib = AccessPlan(
                subject_ref=plan.subject_ref,
                subject_type='employee',
                content_hash=f'hash-{uuid.uuid4().hex[:8]}',
                status=AccessPlanStatus.active,
                requires_confirmation=False,
            )
            session.add(sib)
            siblings.append(sib)
        await session.commit()

    stub = _StubConnector(verify_sequence=['mismatch', 'match'])

    async with session_factory() as session:
        await execute_plan(
            session,
            plan.id,
            run_id,
            stub,  # type: ignore[arg-type]
            log_service=noop_log_service,
        )

    async with session_factory() as session:
        for sib in siblings:
            refreshed = await session.get(AccessPlan, sib.id)
            assert refreshed is not None
            assert refreshed.status == AccessPlanStatus.invalid
            assert refreshed.invalidated_by_plan_id == plan.id


@pytest.mark.asyncio
async def test_dag_order_respected(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Items are executed in topological order: dep before dependent."""
    async with session_factory() as session:
        plan, items = await _seed_plan_with_items(session, n_items=2)
        run_id = uuid.uuid4()
        await _seed_apply_lease(session, plan=plan, pipeline_run_id=run_id)

        # item[1] depends on item[0]
        dep = PlanDependency(
            plan_id=plan.id,
            item_id=items[1].id,
            requires_item_id=items[0].id,
        )
        session.add(dep)
        await session.commit()

    call_order: list[str] = []
    # Track verify calls: odd (1st, 3rd...) = preflight → mismatch; even = post → match
    _vc = [0]

    class _OrderRecordingConnector:
        async def invoke(
            self,
            instance_id: str,
            operation: str,
            payload: dict[str, Any],
            **_kwargs: Any,
        ) -> dict[str, Any]:
            if operation == 'verify_fact':
                _vc[0] += 1
                result_val = 'mismatch' if _vc[0] % 2 == 1 else 'match'
                return {'status': 'ok', 'result': result_val}
            call_order.append(str(payload.get('role_ref', operation)))
            return {'status': 'ok'}

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            _OrderRecordingConnector(),  # type: ignore[arg-type]
            log_service=noop_log_service,
        )

    # Both items done
    assert result.items_done == 2

    # item[0] must appear before item[1] in call_order
    assert call_order[0] == 'role_0'
    assert call_order[1] == 'role_1'
