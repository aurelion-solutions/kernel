# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SyncApplyService — orchestrates apply runs against normalized.access_facts.

Design decisions (Step 12):
- ``apply`` is the single public entry point. All Iceberg writes go through
  ``lake_writer.write_run_batch``; ``SyncApplyService`` never writes directly.
- ``inventory.access_fact.*`` events are emitted ONLY here — not in
  ``inventory/access_facts/service.py``.
- Advisory lock comment (mandatory per TASK.md §3):
  - ``auto_apply`` route handler shares ONE ``AsyncSession`` across
    ``ReconciliationService.run`` and ``SyncApplyService.apply``. The advisory
    lock taken inside ``ReconciliationService.run`` (session-scoped) remains held
    until the session closes, which covers this apply call transparently.
  - Manual apply (``POST /runs/{id}/apply``, separate request) does NOT inherit the
    reconciliation advisory lock. Concurrency is enforced by the idempotency check
    at apply-run level: ``SyncApplyService.apply`` raises
    ``SyncApplyAlreadyExecutedError`` (→ 409) if a running / completed /
    partially_applied apply run already exists for the same
    ``reconciliation_run_id``.
- Transaction discipline: ``apply`` calls ``session.flush()`` via repository helpers
  after each write; the **route owns** ``session.commit()``.
- ``_apply_batch`` ordering invariant (enforced here, validated by grep test):
  1. ``preflight_recover_already_written`` — ALWAYS FIRST, never skipped.
  2. Partition into recovered / to_write.
  3. ``mark_delta_items_applied`` for recovered + ``bulk_insert_results`` (skipping Iceberg).
  4. ``write_run_batch`` for to_write (if any).
  5. ``bulk_insert_results`` for to_write.
  6. Emit ``inventory.access_fact.*`` per item (recovered and freshly written).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.reconciliation.models import ReconciliationDeltaOperation
from src.engines.reconciliation.repository import bulk_approve_run_pending_items
from src.engines.sync_apply.exceptions import (
    SyncApplyAlreadyExecutedError,
    SyncApplyDeltaItemNotApplicableError,
    SyncApplyRunNotFoundError,
)
from src.engines.sync_apply.lake_writer import (
    DenormResolver,
    preflight_recover_already_written,
    write_run_batch,
)
from src.engines.sync_apply.models import (
    SyncApplyResult,
    SyncApplyResultStatus,
    SyncApplyRun,
    SyncApplyRunMode,
    SyncApplyRunStatus,
)
from src.engines.sync_apply.repository import (
    bulk_insert_results,
    create_apply_run,
    get_active_apply_run_for_reconciliation,
    get_non_approved_items,
    get_reconciliation_run,
    list_pending_delta_items,
    mark_delta_items_applied,
    update_apply_run_status,
    update_reconciliation_run_status,
)
from src.engines.sync_apply.schemas import SyncApplyApplyResponse
from src.platform.events.schemas import EventEnvelope, EventParticipantKind, new_event_envelope
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_component_trace_fields

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyiceberg.catalog import Catalog
    from src.engines.reconciliation.models import ReconciliationDeltaItem
    from src.platform.events.service import EventService
    from src.platform.lake.duckdb_session import LakeSession

_COMPONENT = 'engines.sync_apply'


# ---------------------------------------------------------------------------
# Module-level event builders
# (moved from inventory/access_facts/service.py per TASK.md §2 Q1)
# ---------------------------------------------------------------------------


def _build_created_event(
    item: ReconciliationDeltaItem,
    snapshot_id: int | None,
    correlation_id: str,
) -> EventEnvelope:
    """Build ``inventory.access_fact.created`` envelope."""
    payload = item.after_json or {}
    return new_event_envelope(
        event_type='inventory.access_fact.created',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        payload={
            'delta_item_id': str(item.id),
            'reconciliation_run_id': str(item.reconciliation_run_id),
            'snapshot_id': snapshot_id,
            'subject_id': str(item.subject_id),
            'account_id': str(item.account_id) if item.account_id else None,
            'resource_id': str(item.resource_id),
            'action_id': item.action_id,
            'effect': item.effect,
            'natural_key_hash': item.natural_key_hash,
            'valid_from': payload.get('valid_from'),
            'valid_until': payload.get('valid_until'),
            'observed_at': payload.get('observed_at'),
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(item.id),
    )


def _build_updated_event(
    item: ReconciliationDeltaItem,
    snapshot_id: int | None,
    correlation_id: str,
) -> EventEnvelope:
    """Build ``inventory.access_fact.updated`` envelope."""
    return new_event_envelope(
        event_type='inventory.access_fact.updated',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        payload={
            'delta_item_id': str(item.id),
            'reconciliation_run_id': str(item.reconciliation_run_id),
            'snapshot_id': snapshot_id,
            'subject_id': str(item.subject_id),
            'account_id': str(item.account_id) if item.account_id else None,
            'resource_id': str(item.resource_id),
            'action_id': item.action_id,
            'effect': item.effect,
            'natural_key_hash': item.natural_key_hash,
            'before_json': item.before_json,
            'after_json': item.after_json,
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(item.id),
    )


def _build_revoked_event(
    item: ReconciliationDeltaItem,
    snapshot_id: int | None,
    correlation_id: str,
) -> EventEnvelope:
    """Build ``inventory.access_fact.revoked`` envelope."""
    before = item.before_json or {}
    return new_event_envelope(
        event_type='inventory.access_fact.revoked',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        payload={
            'delta_item_id': str(item.id),
            'reconciliation_run_id': str(item.reconciliation_run_id),
            'snapshot_id': snapshot_id,
            'subject_id': str(item.subject_id),
            'resource_id': str(item.resource_id),
            'action_id': item.action_id,
            'effect': item.effect,
            'natural_key_hash': item.natural_key_hash,
            'revoked_at': before.get('revoked_at'),
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(item.id),
    )


def _build_reactivated_event(
    item: ReconciliationDeltaItem,
    snapshot_id: int | None,
    correlation_id: str,
) -> EventEnvelope:
    """Build ``inventory.access_fact.reactivated`` envelope."""
    after = item.after_json or {}
    return new_event_envelope(
        event_type='inventory.access_fact.reactivated',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        payload={
            'delta_item_id': str(item.id),
            'reconciliation_run_id': str(item.reconciliation_run_id),
            'snapshot_id': snapshot_id,
            'subject_id': str(item.subject_id),
            'account_id': str(item.account_id) if item.account_id else None,
            'resource_id': str(item.resource_id),
            'action_id': item.action_id,
            'effect': item.effect,
            'natural_key_hash': item.natural_key_hash,
            'valid_from': after.get('valid_from'),
            'observed_at': after.get('observed_at'),
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(item.id),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SyncApplyService:
    """Orchestrates sync/apply runs against ``normalized.access_facts``.

    Constructor mirrors ``ReconciliationService`` shape; all dependencies are
    required — no defaults. DI lives in ``deps.py``; tests inject fakes directly.

    Advisory lock contract (session-scoped, for ``auto_apply``):
    - When called from the ``auto_apply`` route handler, a **single** FastAPI DI
      ``AsyncSession`` is shared with ``ReconciliationService.run``. The
      ``pg_try_advisory_lock`` taken inside ``ReconciliationService.run`` is
      session-scoped and therefore remains held until the HTTP request releases
      the session — covering this apply call transparently.
    - Manual apply requests (``POST /reconciliation/runs/{id}/apply``) arrive on a
      separate session and do NOT inherit the reconciliation lock. Concurrency is
      enforced by the ``sync_apply_runs.status`` check inside ``apply``.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        lake_session: LakeSession,
        catalog: Catalog,
        denorm_resolver: DenormResolver,
        events: EventService,
        logs: LogService | NoOpLogService,
    ) -> None:
        self._session = session
        self._lake_session = lake_session
        self._catalog = catalog
        self._denorm_resolver = denorm_resolver
        self._events = events
        self._logs: LogService | NoOpLogService = logs

    async def apply(
        self,
        *,
        reconciliation_run_id: UUID,
        mode: SyncApplyRunMode,
        item_ids: list[UUID] | None = None,
        requested_by: str | None = None,
        correlation_id: str | None = None,
    ) -> SyncApplyApplyResponse:
        """Execute an apply run against the reconciliation delta.

        Idempotency at run level: if a ``running | completed | partially_applied``
        apply run already exists for the same ``reconciliation_run_id``,
        ``SyncApplyAlreadyExecutedError`` is raised (→ HTTP 409).

        Crash recovery at item level: ``preflight_recover_already_written`` runs
        FIRST inside ``_apply_batch``; items already in Iceberg but still
        ``approved`` in PG are recovered without a duplicate Iceberg write.

        Transaction discipline: this method calls ``session.flush()`` via repository
        helpers after each write; the **route owns** ``session.commit()``.
        """
        cid = correlation_id if correlation_id is not None else uuid4().hex

        # --- 1. Verify reconciliation run exists ---
        recon_run = await get_reconciliation_run(self._session, reconciliation_run_id)
        if recon_run is None:
            raise SyncApplyRunNotFoundError(reconciliation_run_id)

        # --- 2. Idempotency: reject if an active apply run already exists ---
        existing = await get_active_apply_run_for_reconciliation(self._session, reconciliation_run_id)
        if existing is not None:
            raise SyncApplyAlreadyExecutedError(reconciliation_run_id)

        # --- 3. Create apply run row (status=running) ---
        apply_run = await create_apply_run(
            self._session,
            reconciliation_run_id=reconciliation_run_id,
            mode=mode,
            requested_by=requested_by,
        )

        # --- 4. Load items ---
        items = await self._load_items(reconciliation_run_id, mode, item_ids)

        # --- 5. Log: run started ---
        self._logs.emit_safe(
            level=LogLevel.INFO,
            message='sync_apply.run_started',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {
                    'apply_run_id': str(apply_run.id),
                    'reconciliation_run_id': str(reconciliation_run_id),
                    'mode': mode,
                    'item_count': len(items),
                },
                component_id=_COMPONENT,
                target_id=str(reconciliation_run_id),
            ),
            correlation_id=cid,
        )

        # --- 6. Execute ---
        snapshot_ids: dict[str, int] = {}
        applied_count = 0
        failed_count = 0
        recovered_count = 0

        try:
            if mode == SyncApplyRunMode.dry_run:
                applied_count, failed_count, snapshot_ids, recovered_count = await self._apply_dry_run(
                    self._session,
                    apply_run,
                    items,
                    correlation_id=cid,
                )
            else:
                applied_count, failed_count, snapshot_ids, recovered_count = await self._apply_batch(
                    self._session,
                    apply_run,
                    items,
                    correlation_id=cid,
                )

        except Exception as exc:
            # Update run to failed
            await update_apply_run_status(
                self._session,
                apply_run.id,
                status=SyncApplyRunStatus.failed,
                applied_count=applied_count,
                failed_count=failed_count,
                error=str(exc),
            )
            raise

        # --- 7. Finalize apply run ---
        final_status = SyncApplyRunStatus.completed
        if failed_count > 0 and applied_count > 0:
            final_status = SyncApplyRunStatus.partially_applied
        elif failed_count > 0:
            final_status = SyncApplyRunStatus.failed

        await update_apply_run_status(
            self._session,
            apply_run.id,
            status=final_status,
            applied_count=applied_count,
            failed_count=failed_count,
        )

        # --- 8. Update reconciliation run status ---
        if mode != SyncApplyRunMode.dry_run:
            recon_final = 'applied' if final_status == SyncApplyRunStatus.completed else 'partially_applied'
            await update_reconciliation_run_status(
                self._session,
                reconciliation_run_id,
                status=recon_final,
            )

        # --- 9. Log: run completed ---
        self._logs.emit_safe(
            level=LogLevel.INFO,
            message='sync_apply.run_completed',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {
                    'apply_run_id': str(apply_run.id),
                    'status': final_status,
                    'applied_count': applied_count,
                    'failed_count': failed_count,
                    'recovered_count': recovered_count,
                    'snapshot_ids': snapshot_ids,
                },
                component_id=_COMPONENT,
                target_id=str(reconciliation_run_id),
            ),
            correlation_id=cid,
        )

        return SyncApplyApplyResponse(
            apply_run_id=apply_run.id,
            status=final_status,
            applied_count=applied_count,
            failed_count=failed_count,
            snapshot_ids=snapshot_ids,
        )

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    async def _load_items(
        self,
        reconciliation_run_id: UUID,
        mode: SyncApplyRunMode,
        item_ids: list[UUID] | None,
    ) -> list[ReconciliationDeltaItem]:
        """Load delta items appropriate for the given mode.

        Always bulk-approves pending items first so that explicit Apply actions
        (including selected_items mode) are treated as implicit approval.
        """
        # Promote all pending → approved before any mode-specific loading.
        # Idempotent: items already in approved/applied/failed are unaffected.
        await bulk_approve_run_pending_items(self._session, reconciliation_run_id)

        if mode == SyncApplyRunMode.selected_items:
            assert item_ids, 'item_ids must be non-empty for selected_items mode'
            # After bulk-approve the requested items should all be approved now.
            non_approved = await get_non_approved_items(self._session, reconciliation_run_id, item_ids)
            if non_approved:
                first = non_approved[0]
                raise SyncApplyDeltaItemNotApplicableError(first.id, first.status.value)
            return await list_pending_delta_items(
                self._session,
                reconciliation_run_id,
                item_ids=item_ids,
            )
        return await list_pending_delta_items(self._session, reconciliation_run_id)

    async def _apply_dry_run(
        self,
        session: AsyncSession,
        apply_run: SyncApplyRun,
        items: Sequence[ReconciliationDeltaItem],
        *,
        correlation_id: str,
    ) -> tuple[int, int, dict[str, int], int]:
        """Dry-run: preflight only, mark all results as skipped, no Iceberg writes."""
        # Step 1 (mandatory first): preflight crash-recovery scan
        preflight_recover_already_written(items, lake_session=self._lake_session)

        # Build skipped results
        skipped_results = [
            SyncApplyResult(
                sync_apply_run_id=apply_run.id,
                delta_item_id=item.id,
                status=SyncApplyResultStatus.skipped,
            )
            for item in items
        ]
        if skipped_results:
            await bulk_insert_results(session, skipped_results)

        return 0, 0, {}, 0

    async def _apply_batch(
        self,
        session: AsyncSession,
        apply_run: SyncApplyRun,
        items: Sequence[ReconciliationDeltaItem],
        *,
        correlation_id: str,
    ) -> tuple[int, int, dict[str, int], int]:
        """Orchestrate preflight → recovered → to_write → results → events.

        Ordering invariant (enforced here, validated by architecture test):
        1. ``preflight_recover_already_written`` — ALWAYS FIRST.
        2. Partition items.
        3. Mark recovered items as applied + insert results (no Iceberg write).
        4. ``write_run_batch`` for remaining items (if any).
        5. Insert results for written items.
        6. Emit events for all items (recovered + freshly written).
        """
        now = datetime.now(UTC)

        # --- Step 1: preflight (MUST be first statement) ---
        preflight = preflight_recover_already_written(items, lake_session=self._lake_session)

        # --- Step 2: partition ---
        recovered = [i for i in items if i.id in preflight.recovered_ids]
        to_write = [i for i in items if i.id not in preflight.recovered_ids]

        recovered_count = len(recovered)
        snapshot_ids: dict[str, int] = {}
        applied_count = 0
        failed_count = 0

        # --- Step 3: handle recovered items ---
        if recovered:
            await mark_delta_items_applied(session, [i.id for i in recovered], applied_at=now)
            recovered_results = [
                SyncApplyResult(
                    sync_apply_run_id=apply_run.id,
                    delta_item_id=item.id,
                    status=SyncApplyResultStatus.applied,
                    snapshot_id=None,
                )
                for item in recovered
            ]
            await bulk_insert_results(session, recovered_results)
            applied_count += len(recovered)

        # --- Step 4: write remaining items to Iceberg ---
        if to_write:
            # Pre-load denorm data if the resolver supports it (has .preload attr).
            preload_fn = getattr(self._denorm_resolver, 'preload', None)
            if preload_fn is not None:
                await preload_fn(session, to_write)

            write_result = write_run_batch(
                to_write,
                catalog=self._catalog,
                denorm_resolver=self._denorm_resolver,
                log_service=self._logs,  # type: ignore[arg-type]
            )
            snapshot_ids = write_result.snapshot_ids

            # --- Step 5: persist results for written items ---
            await mark_delta_items_applied(session, [i.id for i in to_write], applied_at=now)

            written_results = []
            for item in to_write:
                op_name = item.operation.value if hasattr(item.operation, 'value') else str(item.operation)
                snap_id = snapshot_ids.get(op_name)
                written_results.append(
                    SyncApplyResult(
                        sync_apply_run_id=apply_run.id,
                        delta_item_id=item.id,
                        status=SyncApplyResultStatus.applied,
                        snapshot_id=snap_id,
                    )
                )
            await bulk_insert_results(session, written_results)
            applied_count += len(to_write)

        # --- Step 6: emit events for all processed items ---
        for item in items:
            if item.id in preflight.recovered_ids:
                item_snap_id = None
            else:
                op_name = item.operation.value if hasattr(item.operation, 'value') else str(item.operation)
                item_snap_id = snapshot_ids.get(op_name)

            try:
                await self._emit_event_for_item(item, item_snap_id, correlation_id)
            except Exception as exc:
                self._logs.emit_safe(
                    level=LogLevel.ERROR,
                    message='sync_apply.item_failed',
                    component=_COMPONENT,
                    payload=merge_emit_component_trace_fields(
                        {
                            'apply_run_id': str(apply_run.id),
                            'delta_item_id': str(item.id),
                            'error': str(exc),
                        },
                        component_id=_COMPONENT,
                        target_id=str(item.reconciliation_run_id),
                    ),
                    correlation_id=correlation_id,
                )

        return applied_count, failed_count, snapshot_ids, recovered_count

    async def _emit_event_for_item(
        self,
        item: ReconciliationDeltaItem,
        snapshot_id: int | None,
        correlation_id: str,
    ) -> None:
        """Dispatch event based on item operation."""
        op = item.operation

        # Normalize to string for comparison
        op_val = op.value if hasattr(op, 'value') else str(op)

        if op_val == ReconciliationDeltaOperation.create.value:
            event = _build_created_event(item, snapshot_id, correlation_id)
        elif op_val == ReconciliationDeltaOperation.update.value:
            event = _build_updated_event(item, snapshot_id, correlation_id)
        elif op_val == ReconciliationDeltaOperation.revoke.value:
            event = _build_revoked_event(item, snapshot_id, correlation_id)
        elif op_val == ReconciliationDeltaOperation.reactivate.value:
            event = _build_reactivated_event(item, snapshot_id, correlation_id)
        else:
            # noop — no event
            return

        await self._events.emit(event)
