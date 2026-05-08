# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""First DB-writing component of the Effective Access Store (Phase 09 Step 3).

``EffectiveAccessProjectionService`` consumes a scope (a single AccessFact id
or an Application id), loads ``(AccessFact, Initiative)`` pairs through the
repository layer, calls the pure ``project()`` function (Step 2), and upserts
the resulting ``EffectiveGrantDraft`` rows into the partitioned
``effective_grants`` table via ``ON CONFLICT ON CONSTRAINT
uq_effective_grants_source_pair``.

Class contract:
- Constructor: ``(session: AsyncSession, event_service: EventService | None = None)``
- ``project_access_fact(*, access_fact_id, now, correlation_id=None)``
- ``project_application(*, application_id, now, correlation_id=None)``
- Both methods return ``ProjectionRunSummary``.

Session discipline (per ARCH_CONTEXT invariant "Transaction ownership"):
- The service calls ``session.flush()`` exactly once, right after the bulk
  upsert, before emitting the event.
- The service NEVER calls ``session.commit()`` or ``session.rollback()``.
- The caller (HTTP handler, event consumer) owns the transaction boundary.

Event emission guarantee:
- Exactly one ``eas.projection.completed`` ``EventEnvelope`` is emitted per
  scope call, post-flush, pre-commit, via ``self._events.emit(...)``.
- On any exception before the flush/emit the event is NOT emitted — this is
  enforced structurally: ``_emit_completed`` is the last statement inside the
  ``try`` body, with no swallowing ``except`` around the upsert or flush.
- Trade-off: if the caller rolls back after ``flush()``, the emitted event
  becomes a "dangling event" for data that never persisted.  This is an
  accepted trade-off (ARCH_CONTEXT §Event emission placement) until Step 5
  gives the service its own transaction boundary via an event consumer.

Error behavior:
- ``ValueError`` from ``project()`` (pair mismatch): emits
  ``eas.projection.failed`` ``EventEnvelope`` then re-raises.
  ``eas.projection.completed`` is NOT emitted on this path.
- ``IntegrityError`` or other DB errors from upsert: bubble up unchanged.

Forbidden in this module: ``print``, ``logging.getLogger``, ``logger.*``,
``structlog``, ``sys.stderr.write``, module-level loggers, ``LogService``,
``emit_safe``, ``LogLevel``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal
import uuid
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.effective_access.models import EffectiveGrant, EffectiveGrantEffect
from src.engines.effective_access.projector import (
    AccessFactView,
    EffectiveGrantDraft,
    InitiativeView,
    project,
)
from src.engines.effective_access.repository import (
    UpsertResult,
    fetch_access_fact_with_initiatives,
    fetch_application_facts_with_initiatives,
    find_grants_for_access,
    get_effective_grant,
    list_effective_grants,
    tombstone_effective_grants_for_access_fact,
    tombstone_effective_grants_for_initiative,
    tombstone_effective_grants_for_missing_pairs,
    upsert_effective_grants,
    upsert_effective_grants_if_observed_at_newer,
)
from src.engines.effective_access.schemas import (
    AccessFactRow,
    EffectiveGrantExplainResult,
    EffectiveGrantRead,
    IncrementalApplyKind,
    InitiativeRow,
    ProjectionRunSummary,
    ProjectionScopeKind,
)
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'engines.effective_access'


class AccessFactNotFoundError(Exception):
    """Raised when the requested AccessFact does not exist."""

    def __init__(self, access_fact_id: UUID) -> None:
        self.access_fact_id = access_fact_id
        super().__init__(f'AccessFact {access_fact_id} not found')


# ---------------------------------------------------------------------------
# Private converters (keep each helper small per coding-python.md)
# ---------------------------------------------------------------------------


def _to_fact_view(row: AccessFactRow) -> AccessFactView:
    """Convert a repository AccessFactRow to the projector's AccessFactView."""
    return AccessFactView(
        id=row.id,
        subject_id=row.subject_id,
        subject_kind=row.subject_kind,
        account_id=row.account_id,
        application_id=row.application_id,
        resource_id=row.resource_id,
        action=row.action,
        effect=row.effect,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
    )


def _to_initiative_view(row: InitiativeRow) -> InitiativeView:
    """Convert a repository InitiativeRow to the projector's InitiativeView."""
    return InitiativeView(
        id=row.id,
        access_fact_id=row.access_fact_id,
        type=row.type,
        origin=row.origin,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
    )


def _build_event_payload(
    summary: ProjectionRunSummary,
    *,
    mode: Literal['batch', 'incremental'],
    change_kind: Literal['upsert', 'invalidate_fact', 'invalidate_initiative'] | None,
    triggered_by: Literal['api', 'consumer'],
    rows_skipped: int = 0,
) -> dict[str, Any]:
    """Build the raw payload dict for the completed event.

    ``causation_event_id`` is NOT included in payload — it lives as a
    first-class ``envelope.causation_id`` field (Phase 10 Step 20, C4).
    """
    return {
        'mode': mode,
        'change_kind': change_kind,
        'scope_kind': summary.scope_kind.value,
        'scope_id': str(summary.scope_id),
        'pairs_projected': summary.pairs_projected,
        'rows_upserted': summary.rows_upserted,
        'rows_inserted': summary.rows_inserted,
        'rows_updated': summary.rows_updated,
        'rows_tombstoned': summary.rows_tombstoned,
        'rows_skipped': rows_skipped,
        'started_at': summary.started_at.isoformat(),
        'finished_at': summary.finished_at.isoformat(),
        'triggered_by': triggered_by,
    }


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class EffectiveAccessProjectionService:
    """Batch projection driver for the Effective Access Store.

    Injects both ``AsyncSession`` and ``EventService`` at construction time so
    tests can supply a ``CapturingEventService`` sink without any monkey-patching.
    ``event_service`` defaults to ``noop_event_service`` when omitted.
    """

    def __init__(self, session: AsyncSession, event_service: EventService | None = None) -> None:
        self._session = session
        self._events = event_service if event_service is not None else noop_event_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def project_access_fact(
        self,
        *,
        access_fact_id: UUID,
        now: datetime,
        correlation_id: UUID | None = None,
    ) -> ProjectionRunSummary:
        """Project all (fact, initiative) pairs for one AccessFact and upsert them.

        Emits one ``eas.projection.completed`` event on success.
        Raises ``AccessFactNotFoundError`` if the fact does not exist.
        """
        corr_id = correlation_id if correlation_id is not None else uuid.uuid4()
        started_at = now

        pair = await fetch_access_fact_with_initiatives(self._session, access_fact_id)
        if pair is None:
            raise AccessFactNotFoundError(access_fact_id)

        fact_row, initiative_rows = pair
        drafts = await self._project_fact(fact_row, initiative_rows, now=now, corr_id=corr_id)

        upsert_result = await upsert_effective_grants(self._session, drafts)
        await self._session.flush()

        finished_at = datetime.now(UTC)
        summary = ProjectionRunSummary(
            scope_kind=ProjectionScopeKind.ACCESS_FACT,
            scope_id=access_fact_id,
            pairs_projected=len(drafts),
            rows_upserted=upsert_result.rows_upserted,
            rows_inserted=upsert_result.rows_inserted,
            rows_updated=upsert_result.rows_updated,
            rows_tombstoned=upsert_result.rows_tombstoned,
            started_at=started_at,
            finished_at=finished_at,
            correlation_id=corr_id,
        )
        await self._emit_completed(summary, mode='batch', change_kind=None, triggered_by='api', rows_skipped=0)
        return summary

    async def project_application(
        self,
        *,
        application_id: UUID,
        now: datetime,
        correlation_id: UUID | None = None,
    ) -> ProjectionRunSummary:
        """Project all (fact, initiative) pairs for one Application and upsert them.

        Streams facts in batches of 500 (keyset-paginated) to avoid loading the
        full fact set into memory.  All batches execute inside the same
        caller-owned transaction — no per-batch savepoints.

        Emits one ``eas.projection.completed`` event on success.
        """
        corr_id = correlation_id if correlation_id is not None else uuid.uuid4()
        started_at = now

        total_pairs = 0
        total_upserted = 0
        total_inserted = 0
        total_updated = 0
        total_tombstoned = 0

        async for fact_row, initiative_rows in fetch_application_facts_with_initiatives(self._session, application_id):
            drafts = await self._project_fact(fact_row, initiative_rows, now=now, corr_id=corr_id)
            if drafts:
                upsert_result = await upsert_effective_grants(self._session, drafts)
                total_pairs += len(drafts)
                total_upserted += upsert_result.rows_upserted
                total_inserted += upsert_result.rows_inserted
                total_updated += upsert_result.rows_updated
                total_tombstoned += upsert_result.rows_tombstoned

        await self._session.flush()

        finished_at = datetime.now(UTC)
        summary = ProjectionRunSummary(
            scope_kind=ProjectionScopeKind.APPLICATION,
            scope_id=application_id,
            pairs_projected=total_pairs,
            rows_upserted=total_upserted,
            rows_inserted=total_inserted,
            rows_updated=total_updated,
            rows_tombstoned=total_tombstoned,
            started_at=started_at,
            finished_at=finished_at,
            correlation_id=corr_id,
        )
        await self._emit_completed(summary, mode='batch', change_kind=None, triggered_by='api', rows_skipped=0)
        return summary

    async def apply_incremental_change(
        self,
        *,
        change_kind: IncrementalApplyKind,
        observed_at: datetime,
        access_fact_id: UUID | None = None,
        initiative_id: UUID | None = None,
        correlation_id: UUID | None = None,
        causation_event_id: UUID | None = None,
    ) -> ProjectionRunSummary:
        """Single-row incremental apply driven by an inventory-side event.

        Three branches, selected by ``change_kind``:

        - ``UPSERT``: fetch fact+initiatives; reuse ``_project_fact`` to build
          drafts; call ``upsert_effective_grants_if_observed_at_newer`` with
          ``observed_at``. If fetch returns ``None`` (fact deleted between
          event emission and consumer invocation), fall through to the
          ``INVALIDATE_FACT`` path. Requires ``access_fact_id``.

          Before the upsert, ``tombstone_effective_grants_for_missing_pairs`` is
          called with ``live_initiative_ids = {d.source_initiative_id for d in drafts}``
          to tombstone any grant whose source initiative has disappeared from the
          live projection (silent-shrink guard — closes the correctness gap where
          initiative deletion does not emit ``initiative.expired``). When ``drafts``
          is empty (fact live but childless), the helper tombstones every grant for
          the fact and the upsert is skipped; the branch remains ``upsert`` in the
          emitted event payload. The set-difference count is folded into
          ``UpsertResult.rows_tombstoned`` before emit.
        - ``INVALIDATE_FACT``: call ``tombstone_effective_grants_for_access_fact``
          with ``observed_at``. No upsert, no ``project()`` call. Requires
          ``access_fact_id``.
        - ``INVALIDATE_INITIATIVE``: call
          ``tombstone_effective_grants_for_initiative`` with ``observed_at``.
          Tombstones only grants whose ``source_initiative_id`` matches.
          Requires ``initiative_id``; ``access_fact_id`` must be ``None``.

        Preconditions (strict XOR — raises ``ValueError`` on violation):
        - ``UPSERT`` / ``INVALIDATE_FACT``: ``access_fact_id`` required,
          ``initiative_id`` must be ``None``.
        - ``INVALIDATE_INITIATIVE``: ``initiative_id`` required,
          ``access_fact_id`` must be ``None``.

        Emits exactly one ``eas.projection.completed`` with
        ``mode='incremental'`` and ``change_kind`` in payload. Session
        discipline: ``flush()`` once before emit; no commit/rollback
        (caller owns txn — the consumer's per-message session).
        """
        # Precondition guard — strict XOR between access_fact_id and initiative_id.
        # Check co-presence (noisier constraint) before absence (missing required id)
        # so the error message names the unexpected argument, not the missing one.
        if change_kind is IncrementalApplyKind.INVALIDATE_INITIATIVE:
            if access_fact_id is not None:
                raise ValueError('INVALIDATE_INITIATIVE must not receive access_fact_id')
            if initiative_id is None:
                raise ValueError('INVALIDATE_INITIATIVE requires initiative_id')
        else:
            if initiative_id is not None:
                raise ValueError(f'{change_kind.value} must not receive initiative_id')
            if access_fact_id is None:
                raise ValueError(f'{change_kind.value} requires access_fact_id')

        corr_id = correlation_id if correlation_id is not None else uuid.uuid4()
        started_at = observed_at

        upsert_result: UpsertResult
        drafts: list[EffectiveGrantDraft] = []
        effective_change_kind = change_kind

        # After the precondition guard above, access_fact_id / initiative_id are
        # guaranteed non-None for their respective branches; use assert for mypy narrowing.
        if change_kind is IncrementalApplyKind.UPSERT:
            assert access_fact_id is not None
            pair = await fetch_access_fact_with_initiatives(self._session, access_fact_id)
            if pair is None:
                # Fact was deleted between event emission and consumer invocation.
                # Fall through to invalidate_fact — idempotent, safe.
                effective_change_kind = IncrementalApplyKind.INVALIDATE_FACT
            else:
                fact_row, initiative_rows = pair
                drafts = await self._project_fact(fact_row, initiative_rows, now=observed_at, corr_id=corr_id)
                live_initiative_ids = {d.source_initiative_id for d in drafts}
                # Step 6b: tombstone grants for initiatives that disappeared from the
                # fact's live set since the last projection (silent-shrink guard).
                # Runs BEFORE the upsert so set-diff reads the pre-upsert state;
                # both statements share the caller-owned transaction.
                diff_tombstoned = await tombstone_effective_grants_for_missing_pairs(
                    self._session,
                    access_fact_id=access_fact_id,
                    observed_at=observed_at,
                    live_initiative_ids=live_initiative_ids,
                )
                if drafts:
                    upsert_result = await upsert_effective_grants_if_observed_at_newer(
                        self._session, drafts, observed_at=observed_at
                    )
                    upsert_result = UpsertResult(
                        rows_upserted=upsert_result.rows_upserted,
                        rows_inserted=upsert_result.rows_inserted,
                        rows_updated=upsert_result.rows_updated,
                        rows_tombstoned=upsert_result.rows_tombstoned + diff_tombstoned,
                        rows_skipped=upsert_result.rows_skipped,
                    )
                else:
                    # Fact is live but has no initiatives — all grants for the fact are
                    # "missing pairs" and were just tombstoned above. Synthesize an
                    # empty-work UpsertResult carrying the diff tombstone count.
                    upsert_result = UpsertResult(
                        rows_upserted=0,
                        rows_inserted=0,
                        rows_updated=0,
                        rows_tombstoned=diff_tombstoned,
                        rows_skipped=0,
                    )

        if effective_change_kind is IncrementalApplyKind.INVALIDATE_FACT:
            assert access_fact_id is not None
            rows_tombstoned = await tombstone_effective_grants_for_access_fact(
                self._session,
                access_fact_id=access_fact_id,
                observed_at=observed_at,
            )
            upsert_result = UpsertResult(
                rows_upserted=0,
                rows_inserted=0,
                rows_updated=0,
                rows_tombstoned=rows_tombstoned,
                rows_skipped=0,
            )

        if change_kind is IncrementalApplyKind.INVALIDATE_INITIATIVE:
            assert initiative_id is not None
            rows_tombstoned = await tombstone_effective_grants_for_initiative(
                self._session,
                initiative_id=initiative_id,
                observed_at=observed_at,
            )
            upsert_result = UpsertResult(
                rows_upserted=0,
                rows_inserted=0,
                rows_updated=0,
                rows_tombstoned=rows_tombstoned,
                rows_skipped=0,
            )

        await self._session.flush()

        # scope_kind and scope_id depend on the branch
        if change_kind is IncrementalApplyKind.INVALIDATE_INITIATIVE:
            assert initiative_id is not None
            scope_kind = ProjectionScopeKind.INITIATIVE
            scope_id: UUID = initiative_id
        else:
            assert access_fact_id is not None
            scope_kind = ProjectionScopeKind.ACCESS_FACT
            scope_id = access_fact_id

        finished_at = datetime.now(UTC)
        summary = ProjectionRunSummary(
            scope_kind=scope_kind,
            scope_id=scope_id,
            pairs_projected=len(drafts),
            rows_upserted=upsert_result.rows_upserted,
            rows_inserted=upsert_result.rows_inserted,
            rows_updated=upsert_result.rows_updated,
            rows_tombstoned=upsert_result.rows_tombstoned,
            rows_skipped=upsert_result.rows_skipped,
            started_at=started_at,
            finished_at=finished_at,
            correlation_id=corr_id,
        )
        await self._emit_completed(
            summary,
            mode='incremental',
            change_kind=effective_change_kind.value,
            triggered_by='consumer',
            rows_skipped=upsert_result.rows_skipped,
            causation_event_id=causation_event_id,
        )
        return summary

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _project_fact(
        self,
        fact_row: AccessFactRow,
        initiative_rows: list[InitiativeRow],
        *,
        now: datetime,
        corr_id: UUID,
    ) -> list[EffectiveGrantDraft]:
        """Call project() for each (fact, initiative) pair; collect drafts.

        On ValueError (pair mismatch) logs eas.projection.failed and re-raises.
        """
        fact_view = _to_fact_view(fact_row)
        drafts: list[EffectiveGrantDraft] = []
        for init_row in initiative_rows:
            init_view = _to_initiative_view(init_row)
            drafts.extend(await self._project_pair(fact_view, init_view, now=now, corr_id=corr_id))
        return drafts

    async def _project_pair(
        self,
        fact_view: AccessFactView,
        initiative_view: InitiativeView,
        *,
        now: datetime,
        corr_id: UUID,
    ) -> list[EffectiveGrantDraft]:
        """Wrap project() — catches ValueError to emit failed event then re-raise."""
        try:
            return project(fact_view, initiative_view, now=now)
        except ValueError:
            await self._events.emit(
                EventEnvelope(
                    event_id=uuid.uuid4(),
                    event_type='eas.projection.failed',
                    occurred_at=datetime.now(UTC),
                    correlation_id=str(corr_id),
                    causation_id=None,
                    payload={
                        'access_fact_id': str(fact_view.id),
                        'initiative_id': str(initiative_view.id),
                        'correlation_id': str(corr_id),
                    },
                    actor_kind=EventParticipantKind.COMPONENT,
                    actor_id=_COMPONENT,
                    target_kind=EventParticipantKind.SYSTEM,
                    target_id=str(fact_view.id),
                )
            )
            raise

    async def _emit_completed(
        self,
        summary: ProjectionRunSummary,
        *,
        mode: Literal['batch', 'incremental'],
        change_kind: Literal['upsert', 'invalidate_fact', 'invalidate_initiative'] | None,
        triggered_by: Literal['api', 'consumer'],
        rows_skipped: int,
        causation_event_id: UUID | None = None,
    ) -> None:
        """Emit eas.projection.completed via EventService.emit."""
        payload = _build_event_payload(
            summary,
            mode=mode,
            change_kind=change_kind,
            triggered_by=triggered_by,
            rows_skipped=rows_skipped,
        )
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='eas.projection.completed',
                occurred_at=datetime.now(UTC),
                correlation_id=str(summary.correlation_id),
                causation_id=causation_event_id,
                payload=payload,
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(summary.scope_id),
            )
        )


# ---------------------------------------------------------------------------
# Step 4 — Read service
# ---------------------------------------------------------------------------


def _aggregate_effect(rows: Sequence[EffectiveGrant]) -> Literal['allow', 'deny', 'none']:
    """Deny-wins aggregation of a row set.

    Read-layer semantics only.  PDP (Phase 06) is authoritative for policy
    decisions; this helper only aggregates matching projection rows.

    - Empty rows → ``'none'``
    - Any row with ``effect=deny`` → ``'deny'`` (deny-wins)
    - All rows ``effect=allow`` → ``'allow'``
    """
    if not rows:
        return 'none'
    if any(r.effect is EffectiveGrantEffect.deny for r in rows):
        return 'deny'
    return 'allow'


class EffectiveAccessReadService:
    """Read-only query driver for the Effective Access Store.

    No events, no flush, no session mutation.  The caller (FastAPI handler)
    owns the session lifecycle; this class only builds queries and returns
    ORM objects.

    Forbidden in this class: ``emit_safe``, ``emit_log``, ``session.flush()``,
    ``session.commit()``, ``session.rollback()``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_grants(
        self,
        *,
        subject_id: UUID | None = None,
        subject_kind: SubjectKind | None = None,
        application_id: UUID | None = None,
        account_id: UUID | None = None,
        resource_id: UUID | None = None,
        action: Action | None = None,
        effect: EffectiveGrantEffect | None = None,
        initiative_type: InitiativeType | None = None,
        initiative_origin: str | None = None,
        source_initiative_id: UUID | None = None,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
        now: datetime | None = None,
    ) -> list[EffectiveGrant]:
        """Pass-through to ``list_effective_grants``."""
        return await list_effective_grants(
            self._session,
            subject_id=subject_id,
            subject_kind=subject_kind,
            application_id=application_id,
            account_id=account_id,
            resource_id=resource_id,
            action=action,
            effect=effect,
            initiative_type=initiative_type,
            initiative_origin=initiative_origin,
            source_initiative_id=source_initiative_id,
            active_only=active_only,
            limit=limit,
            offset=offset,
            now=now,
        )

    async def get_grant(self, grant_id: UUID) -> EffectiveGrant | None:
        """Pass-through to ``get_effective_grant``."""
        return await get_effective_grant(self._session, grant_id)

    async def explain_access(
        self,
        *,
        subject_id: UUID,
        resource_id: UUID,
        action: Action,
        active_only: bool = True,
        now: datetime | None = None,
    ) -> EffectiveGrantExplainResult:
        """Return an ``EffectiveGrantExplainResult`` for the given access triple.

        Computes deny-wins aggregation over matching projection rows.
        ``effect='none'`` when no matching non-tombstoned rows exist.
        This is a READ-LAYER aggregation — not policy evaluation.
        PDP (Phase 06) remains authoritative for allow/deny verdicts.
        """
        rows = await find_grants_for_access(
            self._session,
            subject_id=subject_id,
            resource_id=resource_id,
            action=action,
            active_only=active_only,
            now=now,
        )
        agg = _aggregate_effect(rows)
        grants = [EffectiveGrantRead.model_validate(r) for r in rows]
        return EffectiveGrantExplainResult(effect=agg, grants=grants)
