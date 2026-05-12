# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""F3 post-success chain for access_apply.execute_plan.

Called after a successful verify_fact (either preflight match on executing, or
post-verify match after connector call).  Implements:

Grant path (kind is a positive operation):
  1. inventory_sync.sync_single_fact(descriptor, op=grant, event_key)  — lake write first
  2. initiative.create_or_get(access_fact_id, type, origin, ...)       — PG second (idempotent)

Revoke path (kind is a negative operation):
  1. inventory_sync.sync_single_fact(descriptor, op=revoke, event_key) — lake write first
  2. initiative.close(initiative_id, valid_until=now())                 — PG second (idempotent)
     for each initiative_id in PlanItem.initiative_refs

Atomicity across lake/PG:
- Lake write is first.  If crash happens after lake commit but before PG commit,
  restart (F1 executing→preflight→match) re-enters this function.
- sync_single_fact is wire-level idempotent (event_key check).
- create_or_get / close are idempotent (see InitiativeService docstrings).

The PG writes (Initiative + PlanItemExecution status) must happen in the same
session.commit() call as auto-invalidation (done by execute_plan).  This function
performs lake write then returns; the caller (execute_plan) does the PG commit.

Design note:
  sync_single_fact is a synchronous blocking call (DuckDB + PyIceberg).
  execute_plan wraps it in asyncio.to_thread so it does not block the event loop.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

from src.engines.access_plan.models import PlanItem, PlanItemKind
from src.engines.inventory_sync.schemas import FactDescriptor, SingleFactSyncOp
from src.inventory.initiatives.models import InitiativeType
from src.inventory.initiatives.service import InitiativeService
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_component_trace_fields

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from src.engines.inventory_sync.service import SyncApplyService

_COMPONENT = 'engines.access_apply.f3_chain'

# ---------------------------------------------------------------------------
# Operation classification
# ---------------------------------------------------------------------------

_GRANT_OPS: frozenset[PlanItemKind] = frozenset(
    {
        PlanItemKind.account_create,
        PlanItemKind.account_invite,
        PlanItemKind.account_activate,
        PlanItemKind.grant_role,
        PlanItemKind.group_add,
        PlanItemKind.entitlement_attach,
    }
)

_REVOKE_OPS: frozenset[PlanItemKind] = frozenset(
    {
        PlanItemKind.account_suspend,
        PlanItemKind.account_disable,
        PlanItemKind.revoke_role,
        PlanItemKind.group_remove,
        PlanItemKind.entitlement_detach,
    }
)


def _op_for_item(item: PlanItem) -> SingleFactSyncOp | None:
    """Return grant/revoke op or None if operation has no lake representation."""
    if item.kind in _GRANT_OPS:
        return SingleFactSyncOp.grant
    if item.kind in _REVOKE_OPS:
        return SingleFactSyncOp.revoke
    return None


def _build_descriptor(item: PlanItem) -> FactDescriptor:
    """Build a FactDescriptor from a PlanItem."""
    kind_to_fact_kind = {
        PlanItemKind.grant_role: 'role_grant',
        PlanItemKind.revoke_role: 'role_grant',
        PlanItemKind.group_add: 'group_membership',
        PlanItemKind.group_remove: 'group_membership',
        PlanItemKind.entitlement_attach: 'entitlement',
        PlanItemKind.entitlement_detach: 'entitlement',
        PlanItemKind.account_create: 'account',
        PlanItemKind.account_invite: 'account',
        PlanItemKind.account_activate: 'account',
        PlanItemKind.account_suspend: 'account',
        PlanItemKind.account_disable: 'account',
    }
    fact_kind = kind_to_fact_kind.get(item.kind, item.kind.value)
    return FactDescriptor(
        kind=fact_kind,
        application=item.application,
        target_descriptor=dict(item.target_descriptor),
        account_ref=item.account_ref,
        initiative_refs=[uuid.UUID(ref) for ref in item.initiative_refs] if item.initiative_refs else None,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_f3_chain(
    session: AsyncSession,
    item: PlanItem,
    event_key: str,
    *,
    subject_ref: str,
    subject_type: str,
    sync_service: SyncApplyService,
    initiative_service: InitiativeService,
    log_service: LogService | NoOpLogService,
    correlation_id: str = '',
) -> None:
    """Execute the post-success F3 chain for a single PlanItem.

    Protocol:
    1. Determine op (grant | revoke | skip).
    2. Lake write: sync_single_fact (blocking, wrapped in asyncio.to_thread).
    3. PG write:
       - Grant: create_or_get initiative for each entry in item.initiatives list.
       - Revoke: close initiative for each UUID in item.initiative_refs.

    This function does NOT commit.  The caller (execute_plan) commits as part of
    the per-item PG transaction that also writes PlanItemExecution.status=done and
    runs auto-invalidation.

    Args:
        session: AsyncSession — same session used by execute_plan.
        item: PlanItem whose connector operation just succeeded.
        event_key: Deterministic idempotency key (sha256 of plan_item_id + op).
        subject_ref: Opaque subject identifier from AccessPlan.
        subject_type: 'employee' | 'nhi' from AccessPlan.
        sync_service: SyncApplyService instance (provides sync_single_fact).
        initiative_service: InitiativeService instance.
        log_service: For observability emit_safe calls.
        correlation_id: Trace correlation id.
    """
    op = _op_for_item(item)
    if op is None:
        # No lake representation for this operation kind — skip F3 chain.
        return

    descriptor = _build_descriptor(item)

    # --- Step 1: lake write (blocking I/O in thread) ---
    written = await asyncio.to_thread(
        sync_service.sync_single_fact,
        descriptor,
        op,
        event_key,
        subject_id=uuid.UUID(subject_ref) if _is_uuid(subject_ref) else uuid.uuid5(uuid.NAMESPACE_DNS, subject_ref),
        resource_id=uuid.uuid5(uuid.NAMESPACE_DNS, descriptor.application),
        action_id=item.kind.value,
        application_id_denorm=item.application,
        subject_kind_denorm=subject_type,
        account_id=None,
        correlation_id=correlation_id,
    )

    # allowed-emit-safe: observability
    log_service.emit_safe(
        level=LogLevel.DEBUG,
        message='f3_chain.lake_write',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {
                'item_id': str(item.id),
                'op': op.value,
                'event_key': event_key,
                'written': written,
            },
            component_id=_COMPONENT,
            target_id=str(item.id),
        ),
        correlation_id=correlation_id,
    )

    # --- Step 2: PG write ---
    if op == SingleFactSyncOp.grant:
        await _handle_grant_initiatives(
            session,
            item=item,
            subject_ref=subject_ref,
            subject_type=subject_type,
            initiative_service=initiative_service,
            log_service=log_service,
            correlation_id=correlation_id,
        )
    else:
        await _handle_revoke_initiatives(
            session,
            item=item,
            initiative_service=initiative_service,
            log_service=log_service,
            correlation_id=correlation_id,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_uuid(value: str) -> bool:
    """Return True if value is a valid UUID string."""
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


async def _handle_grant_initiatives(
    session: AsyncSession,
    item: PlanItem,
    *,
    subject_ref: str,
    subject_type: str,
    initiative_service: InitiativeService,
    log_service: LogService | NoOpLogService,
    correlation_id: str,
) -> None:
    """Create (or get) one Initiative per entry in item.initiatives."""
    initiatives_data: list[dict] = item.initiatives or []
    if not initiatives_data:
        return

    # access_fact_id for F3 grant path: use item.id as a stable UUID anchor
    # (access_facts table was dropped in Phase 15 Step 16 — access_fact_id is a
    # plain UUID pointer to the lake event row, not a PG FK).
    access_fact_id = item.id

    for init_data in initiatives_data:
        raw_type = init_data.get('type', 'birthright')
        try:
            type_ = InitiativeType(raw_type)
        except ValueError:
            type_ = InitiativeType.birthright

        origin = str(init_data.get('origin', f'access_apply:{item.application}'))

        valid_from_raw = init_data.get('valid_from')
        valid_until_raw = init_data.get('valid_until')
        valid_from = _parse_dt(valid_from_raw)
        valid_until = _parse_dt(valid_until_raw)

        initiative, created = await initiative_service.create_or_get(
            session,
            access_fact_id=access_fact_id,
            type_=type_,
            origin=origin,
            valid_from=valid_from,
            valid_until=valid_until,
            subject_ref=subject_ref,
            subject_type=subject_type,
            correlation_id=correlation_id,
        )

        # allowed-emit-safe: observability
        log_service.emit_safe(
            level=LogLevel.DEBUG,
            message='f3_chain.initiative_grant',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {
                    'item_id': str(item.id),
                    'initiative_id': str(initiative.id),
                    'created': created,
                    'type': type_.value,
                },
                component_id=_COMPONENT,
                target_id=str(item.id),
            ),
            correlation_id=correlation_id,
        )


async def _handle_revoke_initiatives(
    session: AsyncSession,
    item: PlanItem,
    *,
    initiative_service: InitiativeService,
    log_service: LogService | NoOpLogService,
    correlation_id: str,
) -> None:
    """Close each initiative in item.initiative_refs."""
    refs: list[str] = item.initiative_refs or []
    if not refs:
        return

    close_at = datetime.now(UTC)

    for ref_str in refs:
        try:
            initiative_id = uuid.UUID(ref_str)
        except ValueError:
            # allowed-emit-safe: observability
            log_service.emit_safe(
                level=LogLevel.WARNING,
                message='f3_chain.invalid_initiative_ref',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'item_id': str(item.id), 'ref': ref_str},
                    component_id=_COMPONENT,
                    target_id=str(item.id),
                ),
                correlation_id=correlation_id,
            )
            continue

        try:
            initiative = await initiative_service.close(
                session,
                initiative_id,
                valid_until=close_at,
                correlation_id=correlation_id,
            )
            # allowed-emit-safe: observability
            log_service.emit_safe(
                level=LogLevel.DEBUG,
                message='f3_chain.initiative_closed',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {
                        'item_id': str(item.id),
                        'initiative_id': str(initiative.id),
                        'valid_until': str(close_at),
                    },
                    component_id=_COMPONENT,
                    target_id=str(item.id),
                ),
                correlation_id=correlation_id,
            )
        except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
            # allowed-emit-safe: best-effort warning
            log_service.emit_safe(
                level=LogLevel.WARNING,
                message='f3_chain.initiative_close_failed',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'item_id': str(item.id), 'initiative_id': ref_str},
                    component_id=_COMPONENT,
                    target_id=str(item.id),
                ),
                correlation_id=correlation_id,
            )


def _parse_dt(value: object) -> datetime | None:
    """Parse an ISO datetime string or return None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
