# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""execute_plan — core logic for applying an AccessPlan.

Step F1: iterates PlanItems in topological DAG order, applies each via
connector, verifies result, and persists PlanItemExecution status.

Session contract (Phase 18 compatible):
- Uses ActionContext.session (runner-provided).
- Calls session.commit() after each item — items are independent work units.
- Connector calls happen OUTSIDE active DB transactions (between commits).
- Finally block: DELETE from access_apply_active + commit (lease release).
- On exception in main loop: lease is still released via finally.

F3 chain (Phase 19 Step F3 — fully wired):
- sync_single_fact: lake write via inventory_sync.SyncApplyService (F2).
- Initiative create_or_get (grant path) / close (revoke path) via InitiativeService.
- All F3 operations are idempotent: safe to replay on crash-recovery.
- Lake write is always first; PG commit happens together with status=done.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
from typing import TYPE_CHECKING, Any
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_apply.f3_chain import run_f3_chain
from src.engines.access_plan.models import (
    PlanDependency,
    PlanItem,
    PlanItemExecution,
    PlanItemExecutionStatus,
    PlanItemFailureReason,
)
from src.engines.access_plan.repository import (
    delete_apply_lease,
    fetch_item_executions,
    fetch_plan_by_id,
    fetch_plan_deps,
    fetch_plan_items_ordered,
    invalidate_other_active_plans,
    upsert_item_execution,
)
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_component_trace_fields

if TYPE_CHECKING:
    from src.engines.inventory_sync.service import SyncApplyService
    from src.inventory.initiatives.service import InitiativeService
    from src.platform.connectors.client import ConnectorClient

_COMPONENT = 'engines.access_apply'

# ---------------------------------------------------------------------------
# Verify-fact result constants
# ---------------------------------------------------------------------------

_VERIFY_MATCH = 'match'
_VERIFY_MISMATCH = 'mismatch'
_VERIFY_TIMEOUT = 'timeout'


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


class _ExecutePlanCounts:
    """Internal counts returned from execute_plan to the action handler."""

    __slots__ = ('items_attempted', 'items_done', 'items_failed', 'plan_id')

    def __init__(
        self,
        plan_id: uuid.UUID,
        items_attempted: int,
        items_done: int,
        items_failed: int,
    ) -> None:
        self.plan_id = plan_id
        self.items_attempted = items_attempted
        self.items_done = items_done
        self.items_failed = items_failed


# Public alias
ExecutePlanCounts = _ExecutePlanCounts


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def execute_plan(
    session: AsyncSession,
    plan_id: uuid.UUID,
    pipeline_run_id: uuid.UUID,
    connector: ConnectorClient,
    *,
    log_service: LogService | NoOpLogService,
    sync_service: SyncApplyService | None = None,
    initiative_service: InitiativeService | None = None,
    correlation_id: str | None = None,
) -> ExecutePlanCounts:
    """Apply all items of an AccessPlan, committing per item.

    Session discipline (Phase 18 compatible, relaxed for per-item commits):
    - Runner provides ``session``.
    - This function calls ``session.commit()`` after each item — acceptable
      because execute_plan is a long-lived action that manages its own
      item-level commit boundaries.  The runner's final commit is a no-op
      after this function returns (nothing left uncommitted).
    - Connector calls happen OUTSIDE DB transactions (between commits).
    - Finally block releases the subject-level apply lease.

    F3 chain:
    - When ``sync_service`` and ``initiative_service`` are provided, the F3
      post-success chain runs after each successful verify_fact:
      lake write (sync_single_fact) then PG write (Initiative create_or_get /
      close).  The lake write is always first (crash-safe).
    - When either is None, F3 chain is skipped (used in tests that focus on
      F1 item-level logic only).

    Args:
        session: AsyncSession from ActionContext.
        plan_id: UUID of the AccessPlan to apply.
        pipeline_run_id: UUID used to identify the apply lease row.
        connector: ConnectorClient for invoking operations and verify_fact.
        log_service: For observability emit_safe calls.
        sync_service: Optional SyncApplyService for lake writes (F3).
        initiative_service: Optional InitiativeService for PG initiative writes (F3).
        correlation_id: Optional trace correlation id.
    """
    cid = correlation_id or uuid.uuid4().hex

    plan = await fetch_plan_by_id(session, plan_id)
    if plan is None:
        raise PlanNotFoundError(plan_id)

    items = await fetch_plan_items_ordered(session, plan_id)
    deps = await fetch_plan_deps(session, plan_id)
    executions = await fetch_item_executions(session, plan_id)

    # Ensure PlanItemExecution rows exist for all items (proposed by default).
    for item in items:
        if item.id not in executions:
            await upsert_item_execution(
                session,
                plan_id=plan_id,
                item_id=item.id,
                status=PlanItemExecutionStatus.proposed,
            )
    await session.commit()
    # Reload executions after ensuring rows exist
    executions = await fetch_item_executions(session, plan_id)

    topo_order = _topological_sort(items, deps)

    items_by_id = {item.id: item for item in items}

    items_attempted = 0
    items_done = 0
    items_failed = 0

    try:
        for item_id in topo_order:
            item = items_by_id[item_id]
            execution = executions.get(item_id)
            prev_status = execution.status if execution else PlanItemExecutionStatus.proposed

            if prev_status == PlanItemExecutionStatus.done:
                items_done += 1
                continue

            items_attempted += 1

            # --- preflight verify_fact ---
            preflight_result = await _verify_fact(connector, item, cid)

            if preflight_result == _VERIFY_MATCH:
                # Target already in desired state.
                if prev_status == PlanItemExecutionStatus.executing:
                    # Crash recovery: connector succeeded but kernel crashed before
                    # writing done.  Re-run F3 chain idempotently.
                    if sync_service is not None and initiative_service is not None:
                        await run_f3_chain(
                            session,
                            item,
                            _make_event_key(item_id, item.kind.value),
                            subject_ref=plan.subject_ref,
                            subject_type=plan.subject_type,
                            sync_service=sync_service,
                            initiative_service=initiative_service,
                            log_service=log_service,
                            correlation_id=cid,
                        )
                    await invalidate_other_active_plans(session, plan.subject_ref, plan_id)

                await upsert_item_execution(
                    session,
                    plan_id=plan_id,
                    item_id=item_id,
                    status=PlanItemExecutionStatus.done,
                )
                await session.commit()
                executions[item_id] = await _fetch_single_execution(session, plan_id, item_id)
                items_done += 1
                continue

            # --- transition to executing ---
            await upsert_item_execution(
                session,
                plan_id=plan_id,
                item_id=item_id,
                status=PlanItemExecutionStatus.executing,
            )
            await session.commit()
            executions[item_id] = await _fetch_single_execution(session, plan_id, item_id)

            # --- call connector (outside DB tx) ---
            connector_error: str | None = None
            try:
                await _call_connector(connector, item, cid)
            except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
                connector_error = str(exc)

            if connector_error is not None:
                await upsert_item_execution(
                    session,
                    plan_id=plan_id,
                    item_id=item_id,
                    status=PlanItemExecutionStatus.failed,
                    failure_reason=PlanItemFailureReason.apply_error,
                    last_error=connector_error,
                )
                await session.commit()
                executions[item_id] = await _fetch_single_execution(session, plan_id, item_id)
                items_failed += 1
                # allowed-emit-safe: observability
                log_service.emit_safe(
                    level=LogLevel.WARNING,
                    message='execute_plan.item_connector_error',
                    component=_COMPONENT,
                    payload=merge_emit_component_trace_fields(
                        {
                            'plan_id': str(plan_id),
                            'item_id': str(item_id),
                            'kind': item.kind.value,
                            'error': connector_error,
                        },
                        component_id=_COMPONENT,
                        target_id=str(plan_id),
                    ),
                    correlation_id=cid,
                )
                continue

            # --- post verify_fact ---
            post_result = await _verify_fact(connector, item, cid)

            if post_result == _VERIFY_MATCH:
                # F3 chain: lake write first, then PG initiative writes.
                if sync_service is not None and initiative_service is not None:
                    await run_f3_chain(
                        session,
                        item,
                        _make_event_key(item_id, item.kind.value),
                        subject_ref=plan.subject_ref,
                        subject_type=plan.subject_type,
                        sync_service=sync_service,
                        initiative_service=initiative_service,
                        log_service=log_service,
                        correlation_id=cid,
                    )
                await invalidate_other_active_plans(session, plan.subject_ref, plan_id)
                await upsert_item_execution(
                    session,
                    plan_id=plan_id,
                    item_id=item_id,
                    status=PlanItemExecutionStatus.done,
                )
                await session.commit()
                executions[item_id] = await _fetch_single_execution(session, plan_id, item_id)
                items_done += 1
            else:
                failure_reason = (
                    PlanItemFailureReason.verify_timeout
                    if post_result == _VERIFY_TIMEOUT
                    else PlanItemFailureReason.verify_mismatch
                )
                await upsert_item_execution(
                    session,
                    plan_id=plan_id,
                    item_id=item_id,
                    status=PlanItemExecutionStatus.failed,
                    failure_reason=failure_reason,
                    last_error=f'post-verify: {post_result}',
                )
                await session.commit()
                executions[item_id] = await _fetch_single_execution(session, plan_id, item_id)
                items_failed += 1

    finally:
        # Release subject-level apply lease — guaranteed even on exception.
        await delete_apply_lease(session, pipeline_run_id)
        await session.commit()

    return ExecutePlanCounts(
        plan_id=plan_id,
        items_attempted=items_attempted,
        items_done=items_done,
        items_failed=items_failed,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class PlanNotFoundError(Exception):
    """Raised when the plan does not exist."""

    def __init__(self, plan_id: uuid.UUID) -> None:
        self.plan_id = plan_id
        super().__init__(f'AccessPlan not found: {plan_id}')


def _topological_sort(
    items: list[PlanItem],
    deps: list[PlanDependency],
) -> list[uuid.UUID]:
    """Kahn's topological sort on PlanDependency edges.

    Items with no deps come first. If a cycle exists (should not after D4),
    items involved are appended at the end (fail-loud by including them so
    they can be attempted and surface failures).
    """
    all_ids = {item.id for item in items}
    # in-edges: item_id -> set of requires_item_id that must run first
    in_edges: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    # out-edges: requires_item_id -> set of item_ids that depend on it
    out_edges: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)

    for dep in deps:
        if dep.item_id in all_ids and dep.requires_item_id in all_ids:
            in_edges[dep.item_id].add(dep.requires_item_id)
            out_edges[dep.requires_item_id].add(dep.item_id)

    queue = [item_id for item_id in all_ids if not in_edges[item_id]]
    queue.sort()  # stable order for items with no deps
    result: list[uuid.UUID] = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        for dependent in sorted(out_edges[node]):
            in_edges[dependent].discard(node)
            if not in_edges[dependent]:
                queue.append(dependent)

    # Append any remaining (cycle nodes) so they can fail gracefully
    remaining = [item_id for item_id in all_ids if item_id not in set(result)]
    result.extend(sorted(remaining))

    return result


def _make_event_key(item_id: uuid.UUID, op_suffix: str) -> str:
    """Deterministic event key for a plan item operation — used by F3 chain."""
    return hashlib.sha256(f'{item_id}:{op_suffix}'.encode()).hexdigest()[:32]


def _build_verify_payload(item: PlanItem) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build (descriptor, expected_state) for verify_fact from a PlanItem."""
    kind = item.kind.value
    target = dict(item.target_descriptor)

    # Infer fact kind from operation kind
    if kind.startswith('account_'):
        fact_kind = 'account'
        account_ref = item.account_ref or target.get('account_ref', '')
        descriptor = {'kind': fact_kind, 'account_ref': account_ref, **target}
        expected_status = _expected_account_status(kind)
        expected_state: dict[str, Any] = {'status': expected_status}
    elif kind in ('grant_role', 'revoke_role'):
        fact_kind = 'role'
        account_ref = item.account_ref or target.get('account_ref', '')
        descriptor = {'kind': fact_kind, 'account_ref': account_ref, **target}
        expected_state = {'present': kind == 'grant_role'}
    elif kind in ('group_add', 'group_remove'):
        fact_kind = 'group'
        account_ref = item.account_ref or target.get('account_ref', '')
        descriptor = {'kind': fact_kind, 'account_ref': account_ref, **target}
        expected_state = {'present': kind == 'group_add'}
    elif kind in ('entitlement_attach', 'entitlement_detach'):
        fact_kind = 'entitlement'
        account_ref = item.account_ref or target.get('account_ref', '')
        descriptor = {'kind': fact_kind, 'account_ref': account_ref, **target}
        expected_state = {'present': kind == 'entitlement_attach'}
    else:
        # Unknown kind — use target descriptor as-is
        descriptor = {'kind': kind, **target}
        expected_state = {'present': True}

    return descriptor, expected_state


def _expected_account_status(kind: str) -> str:
    """Map account operation kind to expected post-operation status."""
    mapping = {
        'account_create': 'active',
        'account_invite': 'invited',
        'account_activate': 'active',
        'account_suspend': 'suspended',
        'account_disable': 'disabled',
    }
    return mapping.get(kind, 'active')


async def _verify_fact(
    connector: ConnectorClient,
    item: PlanItem,
    correlation_id: str,
) -> str:
    """Call connector verify_fact and return match|mismatch|timeout."""
    descriptor, expected_state = _build_verify_payload(item)
    try:
        result = await connector.invoke(
            instance_id=item.application,
            operation='verify_fact',
            payload={'descriptor': descriptor, 'expected_state': expected_state},
            correlation_id=correlation_id,
        )
        return str(result.get('result', _VERIFY_MISMATCH))
    except Exception:  # noqa: BLE001 # allowed-broad: provider boundary
        # Network error / timeout treated as mismatch so connector is retried
        return _VERIFY_MISMATCH


async def _call_connector(
    connector: ConnectorClient,
    item: PlanItem,
    correlation_id: str,
) -> dict[str, Any]:
    """Invoke the connector operation for a PlanItem."""
    payload: dict[str, Any] = dict(item.target_descriptor)
    if item.account_ref:
        payload['account_ref'] = item.account_ref
    return await connector.invoke(
        instance_id=item.application,
        operation=item.kind.value,
        payload=payload,
        correlation_id=correlation_id,
    )


async def _fetch_single_execution(
    session: AsyncSession,
    plan_id: uuid.UUID,
    item_id: uuid.UUID,
) -> PlanItemExecution:  # type: ignore[return]
    """Re-fetch a single PlanItemExecution row after commit."""
    result = await session.execute(
        sa.select(PlanItemExecution).where(
            PlanItemExecution.plan_id == plan_id,
            PlanItemExecution.item_id == item_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise RuntimeError(f'PlanItemExecution not found after upsert: plan={plan_id}, item={item_id}')
    return row  # type: ignore[return-value]
