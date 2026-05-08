# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Artifact-first reconciliation pipeline — Phase 15 Step 8 rewrite.

Entry point: ``run_reconciliation(session, lake_session, catalog, *, application_id)``.

Pipeline phases:
  1. ``_phase_load_artifacts``      — DuckDB iceberg_scan raw.access_artifacts
  2. ``_phase_dispatch``            — handler dispatch → NormalizationResult candidates
  3. ``_phase_load_current_state``  — DuckDB iceberg_scan normalized.access_facts
  4. ``_phase_resolve_action_ids``  — DuckDB ref_actions_local TEMP TABLE lookup
  5. ``_phase_persist_delta``       — compute set-diff + write ReconciliationDeltaItems

Deleted phases (Step 8):
  - ``_phase_apply_delta`` — replaced by ``_phase_persist_delta``; no fact mutation here.
  - ``_write_binding``, ``_require_subject_or_fail`` — belonged to apply phase.

Design decisions:
  - NO writes to ``normalized.access_facts`` in this step (Steps 11–12 own that).
  - NO calls to ``AccessFactService.*`` in this module.
  - NO logging here — Step 9 adds LogService call sites.
  - NO events here — Step 9 adds event emission.
  - Caller (route or CLI) commits the transaction; this module only flushes.

Known race (documented):
  Snapshot ids are captured via ``catalog.load_table(...).current_snapshot()``
  BEFORE running DuckDB queries.  If a writer commits a new snapshot between the
  capture and the scan, DuckDB may read a newer snapshot than the recorded id.
  Mitigated by the single-writer kernel invariant and Step 9 advisory lock.
  Revisit if concurrent writes are ever allowed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.reconciliation.contracts import NormalizationResult
from src.engines.reconciliation.hashing import compute_natural_key_hash
from src.engines.reconciliation.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
    ReconciliationRunStatus,
)
from src.engines.reconciliation.registry import get_handler
from src.engines.reconciliation.repository import (
    RunCounts,
    bulk_insert_delta_items,
    create_run,
    update_run_status,
)
from src.engines.reconciliation.views import AccessArtifactRowView, AccessFactRowView
from src.inventory.access_artifacts.schemas import AccessArtifactView
from src.platform.lake.duckdb_session import LakeSession

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog
    from src.engines.reconciliation.schemas import ReconciliationRunSummary

# (subject_id | None, account_id | None, resource_id, action_id)
FactKey = tuple[UUID | None, UUID | None, UUID, int]

# Artifact row DTO + NormalizationResult pair.
# The result may be None when a handler raised an exception (None sentinel).
# _phase_dispatch adapts AccessArtifactRowView → AccessArtifactView before handler call.
_NormalizedCandidate = tuple[AccessArtifactRowView, NormalizationResult | None]

# Resolved candidate: DTO, result, action_id (resolved), fact_key
_ResolvedCandidate = tuple[AccessArtifactRowView, NormalizationResult, int, FactKey]

# Batch size for DuckDB fetchmany iterations.
# TODO: move to LakeSettings in a later step.
_FETCH_BATCH_SIZE = 5000


# ---------------------------------------------------------------------------
# Natural key hash — delegated to shared helper (Phase 15 Step 14 D5 refactor).
# compute_natural_key_hash is imported from src.engines.reconciliation.hashing.
# The private alias below allows existing call sites in this module to remain unchanged
# without a sweeping rename refactor.
_compute_natural_key_hash = compute_natural_key_hash


# ---------------------------------------------------------------------------
# DTO adapter
# ---------------------------------------------------------------------------


def _to_view(row: AccessArtifactRowView) -> AccessArtifactView:
    """Adapt AccessArtifactRowView (pipeline-local) to AccessArtifactView (handler contract).

    Key difference: AccessArtifactRowView.payload is str | None (JSON-encoded from lake);
    AccessArtifactView.payload is dict[str, Any]. json.loads is required here.
    ingest_batch_id is cast UUID | None → str | None.
    tombstoned_at and ingested_at are absent in AccessArtifactRowView (not read by
    reconciliation); they are defaulted to None / sentinel datetime respectively.
    """
    import json

    payload_dict: dict[str, Any]
    if row.payload is None:
        payload_dict = {}
    else:
        # row.payload is str (JSON-encoded from lake column)
        try:
            loaded = json.loads(row.payload)
            payload_dict = loaded if isinstance(loaded, dict) else {}
        except (ValueError, TypeError):
            payload_dict = {}

    ingest_batch_id_str: str | None = str(row.ingest_batch_id) if row.ingest_batch_id is not None else None

    return AccessArtifactView(
        id=row.id,
        application_id=row.application_id,
        artifact_type=row.artifact_type,
        external_id=row.external_id,
        payload=payload_dict,
        raw_name=row.raw_name,
        effect=row.effect,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        is_active=row.is_active,
        tombstoned_at=None,
        observed_at=row.observed_at,
        ingested_at=row.observed_at,  # reconciliation doesn't read ingested_at; use observed_at as sentinel
        ingest_batch_id=ingest_batch_id_str,
    )


# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------


def _phase_load_artifacts(
    lake_session: LakeSession,
    *,
    application_id: UUID,
    batch_size: int = _FETCH_BATCH_SIZE,
) -> list[AccessArtifactRowView]:
    """Load active artifacts for the application via DuckDB iceberg_scan.

    SQL reads ``raw.access_artifacts`` filtered by ``application_id`` and
    ``is_active=true``.  Rows are streamed via ``fetchmany(batch_size)`` to
    avoid materialising the full result set into Python heap.
    """
    table_path = lake_session.iceberg_table_path('raw', 'access_artifacts')
    # application_id is stored as StringType in raw.access_artifacts — compare as plain string.
    sql = (
        'SELECT id, application_id, artifact_type, external_id, payload, raw_name, '
        '       effect, valid_from, valid_until, is_active, observed_at, ingest_batch_id '
        f"FROM iceberg_scan('{table_path}') "
        'WHERE application_id = ? AND is_active = true'
    )
    lake_session.execute(sql, [str(application_id)])

    rows: list[AccessArtifactRowView] = []
    while True:
        batch = lake_session._conn.fetchmany(batch_size)
        if not batch:
            break
        for row in batch:
            (
                r_id,
                r_application_id,
                r_artifact_type,
                r_external_id,
                r_payload,
                r_raw_name,
                r_effect,
                r_valid_from,
                r_valid_until,
                r_is_active,
                r_observed_at,
                r_ingest_batch_id,
            ) = row
            rows.append(
                AccessArtifactRowView(
                    id=UUID(str(r_id)) if not isinstance(r_id, UUID) else r_id,
                    application_id=UUID(str(r_application_id))
                    if not isinstance(r_application_id, UUID)
                    else r_application_id,
                    artifact_type=r_artifact_type,
                    external_id=r_external_id,
                    payload=r_payload,
                    raw_name=r_raw_name,
                    effect=r_effect,
                    valid_from=r_valid_from,
                    valid_until=r_valid_until,
                    is_active=r_is_active,
                    observed_at=r_observed_at,
                    ingest_batch_id=UUID(str(r_ingest_batch_id)) if r_ingest_batch_id is not None else None,
                )
            )
    return rows


async def _phase_dispatch(
    session: AsyncSession,
    artifacts: list[AccessArtifactRowView],
) -> tuple[list[_NormalizedCandidate], int]:
    """Dispatch each artifact DTO to its registered handler.

    AccessArtifactRowView is adapted to AccessArtifactView (handler contract) via
    _to_view before each handler call. The handler Protocol expects AccessArtifactView
    (Phase 15 Step 16).

    Returns ``(candidates, unhandled_count)``.
    Errored artifacts (handler raised) are appended as ``(artifact, None)`` sentinel.
    """
    candidates: list[_NormalizedCandidate] = []
    unhandled = 0

    for artifact in artifacts:
        handler = get_handler(artifact.artifact_type)
        if handler is None:
            unhandled += 1
            continue
        try:
            view = _to_view(artifact)
            results = await handler.handle(view, session)
        except Exception:
            # Per-artifact exception — signal errored via None sentinel.
            # Caller tracks facts_errored; no logging here (Step 9 adds LogService).
            candidates.append((artifact, None))
            continue
        for result in results:
            candidates.append((artifact, result))

    return candidates, unhandled


def _phase_load_current_state(
    lake_session: LakeSession,
    *,
    application_id: UUID,
    batch_size: int = _FETCH_BATCH_SIZE,
) -> tuple[set[FactKey], dict[FactKey, AccessFactRowView]]:
    """Load active facts for the application via DuckDB iceberg_scan.

    SQL reads ``normalized.access_facts`` filtered by ``application_id_denorm``
    (the partition column) and ``is_active=true``.  No PG join required.

    ``action_id`` is stored as ``StringType`` in the Iceberg schema — coerced to
    ``int`` here so ``FactKey`` stays ``int``-typed.
    """
    table_path = lake_session.iceberg_table_path('normalized', 'access_facts')
    sql = (
        'SELECT id, subject_id, account_id, resource_id, action_id, effect, '
        '       valid_from, valid_until, is_active, observed_at, natural_key_hash '
        f"FROM iceberg_scan('{table_path}') "
        'WHERE application_id_denorm = ? AND is_active = true'
    )
    lake_session.execute(sql, [str(application_id)])

    def _as_uuid(v: object) -> UUID | None:
        if v is None:
            return None
        return v if isinstance(v, UUID) else UUID(str(v))

    current_keys: set[FactKey] = set()
    current_rows: dict[FactKey, AccessFactRowView] = {}

    while True:
        batch = lake_session._conn.fetchmany(batch_size)
        if not batch:
            break
        for row in batch:
            (
                r_id,
                r_subject_id,
                r_account_id,
                r_resource_id,
                r_action_id,
                r_effect,
                r_valid_from,
                r_valid_until,
                r_is_active,
                r_observed_at,
                r_natural_key_hash,
            ) = row
            view = AccessFactRowView(
                id=_as_uuid(r_id),  # type: ignore[arg-type]
                subject_id=_as_uuid(r_subject_id),
                account_id=_as_uuid(r_account_id),
                resource_id=_as_uuid(r_resource_id),  # type: ignore[arg-type]
                action_id=int(r_action_id),
                effect=r_effect,
                valid_from=r_valid_from,
                valid_until=r_valid_until,
                is_active=r_is_active,
                observed_at=r_observed_at,
                natural_key_hash=r_natural_key_hash,
            )
            key: FactKey = (view.subject_id, view.account_id, view.resource_id, view.action_id)
            current_keys.add(key)
            current_rows[key] = view

    return current_keys, current_rows


async def _phase_resolve_action_ids(
    lake_session: LakeSession,
    candidates: list[_NormalizedCandidate],
) -> tuple[list[_ResolvedCandidate], int]:
    """Resolve action slugs → action ids via ``ref_actions_local`` TEMP TABLE.

    Pattern-3 (phase_15 Step 1 Key decisions): ``ref_actions_local`` is a DuckDB
    TEMP TABLE materialised once per session from PG ``ref_actions`` at open time.
    Unknown slugs → errored_count incremented, candidate skipped.
    Errored candidates (handler exception, None result) → skipped + counted.
    """
    # Collect unique slugs (skip errored / None results)
    slugs: set[str] = set()
    for _artifact, result in candidates:
        if result is not None:
            slugs.add(result.action_slug)

    slug_to_id: dict[str, int] = {}
    if slugs:
        placeholders = ', '.join('?' for _ in slugs)
        sql = f'SELECT slug, id FROM ref_actions_local WHERE slug IN ({placeholders})'
        lake_session.execute(sql, list(slugs))
        for slug_val, action_id_val in lake_session.fetchall():
            slug_to_id[slug_val] = int(action_id_val)

    resolved: list[_ResolvedCandidate] = []
    errored = 0

    for artifact, result in candidates:
        if result is None:
            # Handler raised an exception — signalled via None sentinel in _phase_dispatch.
            errored += 1
            continue
        action_id = slug_to_id.get(result.action_slug)
        if action_id is None:
            # Unknown slug — skip without logging (Step 9 adds LogService)
            errored += 1
            continue
        key: FactKey = (result.subject_id, result.account_id, result.resource_id, action_id)
        resolved.append((artifact, result, action_id, key))

    return resolved, errored


def _build_snapshot_json(view: AccessFactRowView) -> dict[str, Any]:
    """Build a plain-dict snapshot from an AccessFactRowView for before/after JSON."""
    return {
        'subject_id': str(view.subject_id),
        'account_id': str(view.account_id) if view.account_id is not None else None,
        'resource_id': str(view.resource_id),
        'action_id': view.action_id,
        'effect': view.effect,
        'valid_from': view.valid_from.isoformat() if view.valid_from is not None else None,
        'valid_until': view.valid_until.isoformat() if view.valid_until is not None else None,
        'is_active': view.is_active,
    }


def _build_candidate_json(result: NormalizationResult, action_id: int, is_active: bool) -> dict[str, Any]:
    """Build a plain-dict snapshot from a resolved NormalizationResult for after JSON."""
    return {
        'subject_id': str(result.subject_id) if result.subject_id is not None else None,
        'account_id': str(result.account_id) if result.account_id is not None else None,
        'resource_id': str(result.resource_id),
        'action_id': action_id,
        'effect': result.effect,
        'valid_from': result.valid_from.isoformat() if result.valid_from is not None else None,
        'valid_until': result.valid_until.isoformat() if result.valid_until is not None else None,
        'is_active': is_active,
    }


async def _phase_persist_delta(
    session: AsyncSession,
    *,
    run_id: UUID,
    resolved_candidates: list[_ResolvedCandidate],
    current_keys: set[FactKey],
    current_rows: dict[FactKey, AccessFactRowView],
    application_id: UUID,
    run_started_at: datetime,
) -> tuple[int, int, int, int]:
    """Compute set-diff and persist ``ReconciliationDeltaItem`` rows to PG.

    Never calls ``AccessFactService.*`` — this is comparison-only.
    Never writes to ``normalized.access_facts``.

    Write-amplification guard: unchanged keys (key in both sets, no field drift)
    are NOT inserted as rows.  ``unchanged_count`` is returned for the run summary
    but no delta item row is created.  This prevents the delta_items table from
    accumulating rows for every reconciliation cycle even when nothing changed.

    ``noop`` operation is never emitted in this step (only makes sense if we
    materialised unchanged rows, which we don't).

    Returns ``(created, updated, revoked, unchanged)``.
    """
    # Build new key set and key → (artifact, result, action_id) map
    new_keys: set[FactKey] = set()
    key_to_candidate: dict[FactKey, tuple[AccessArtifactRowView, NormalizationResult, int]] = {}

    for artifact, result, action_id, key in resolved_candidates:
        new_keys.add(key)
        key_to_candidate[key] = (artifact, result, action_id)

    created_keys = new_keys - current_keys
    revoked_keys = current_keys - new_keys
    common_keys = new_keys & current_keys

    items: list[ReconciliationDeltaItem] = []
    created_count = 0
    updated_count = 0
    revoked_count = 0
    unchanged_count = 0

    # CREATE
    for key in created_keys:
        artifact, result, action_id = key_to_candidate[key]
        subject_id = result.subject_id or key[0]
        if subject_id is None and result.account_id is None:
            # Both subject_id and account_id are None — bad handler output, skip
            continue
        natural_key_hash = _compute_natural_key_hash(
            application_id, subject_id, result.account_id, result.resource_id, action_id, result.effect
        )
        items.append(
            ReconciliationDeltaItem(
                reconciliation_run_id=run_id,
                entity_type=ReconciliationEntityType.access_fact,
                operation=ReconciliationDeltaOperation.create,
                natural_key_hash=natural_key_hash,
                subject_id=subject_id,
                account_id=result.account_id,
                resource_id=result.resource_id,
                action_id=action_id,
                effect=result.effect,
                source_artifact_id=artifact.id,
                before_json=None,
                after_json=_build_candidate_json(result, action_id, is_active=True),
            )
        )
        created_count += 1

    # UPDATE (field drift on common keys)
    for key in common_keys:
        artifact, result, action_id = key_to_candidate[key]
        current_fact = current_rows[key]

        vf_differs = result.valid_from is not None and current_fact.valid_from != result.valid_from
        fields_differ = (
            current_fact.effect != result.effect or vf_differs or current_fact.valid_until != result.valid_until
        )

        if not fields_differ:
            unchanged_count += 1
            continue

        subject_id = result.subject_id or key[0]
        if subject_id is None and result.account_id is None:
            unchanged_count += 1
            continue

        natural_key_hash = _compute_natural_key_hash(
            application_id, subject_id, result.account_id, result.resource_id, action_id, result.effect
        )
        items.append(
            ReconciliationDeltaItem(
                reconciliation_run_id=run_id,
                entity_type=ReconciliationEntityType.access_fact,
                operation=ReconciliationDeltaOperation.update,
                natural_key_hash=natural_key_hash,
                subject_id=subject_id,
                account_id=result.account_id,
                resource_id=result.resource_id,
                action_id=action_id,
                effect=result.effect,
                existing_fact_id=current_fact.id,
                source_artifact_id=artifact.id,
                before_json=_build_snapshot_json(current_fact),
                after_json=_build_candidate_json(result, action_id, is_active=True),
            )
        )
        updated_count += 1

    # REVOKE
    for key in revoked_keys:
        current_fact = current_rows[key]
        natural_key_hash = _compute_natural_key_hash(
            application_id,
            current_fact.subject_id,
            current_fact.account_id,
            current_fact.resource_id,
            current_fact.action_id,
            current_fact.effect,
        )
        items.append(
            ReconciliationDeltaItem(
                reconciliation_run_id=run_id,
                entity_type=ReconciliationEntityType.access_fact,
                operation=ReconciliationDeltaOperation.revoke,
                natural_key_hash=natural_key_hash,
                subject_id=current_fact.subject_id,
                account_id=current_fact.account_id,
                resource_id=current_fact.resource_id,
                action_id=current_fact.action_id,
                effect=current_fact.effect,
                existing_fact_id=current_fact.id,
                before_json=_build_snapshot_json(current_fact),
                after_json=None,
            )
        )
        revoked_count += 1

    if items:
        await bulk_insert_delta_items(session, items)

    return created_count, updated_count, revoked_count, unchanged_count


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def run_reconciliation(
    session: AsyncSession,
    lake_session: LakeSession,
    catalog: Catalog,
    *,
    application_id: UUID,
    correlation_id: str | None = None,
) -> ReconciliationRunSummary:
    """Run the artifact-first reconciliation pipeline for one application.

    Orchestrates five phases:
      1. Capture Iceberg snapshot ids BEFORE running queries.
      2. Create a ``ReconciliationRun`` row (status=running).
      3. Load artifacts, dispatch handlers, load current state,
         resolve action ids, persist delta.
      4. On success: update run to ``pending_apply`` with counts.
      5. On failure: update run to ``failed`` with error string; re-raise.

    Does NOT commit — caller (route or CLI) owns the transaction boundary.

    Known race: snapshot ids captured here may lag if a writer commits a new
    snapshot between capture and the DuckDB scan.  Mitigated by single-writer
    kernel invariant + Step 9 advisory lock.  Documented here; revisit if
    concurrent lake writes are introduced.
    """
    from src.engines.reconciliation.schemas import ReconciliationRunSummary
    from src.platform.lake.schemas import (
        NORMALIZED_ACCESS_FACTS_TABLE,
        RAW_ACCESS_ARTIFACTS_TABLE,
    )

    run_started_at = datetime.now(UTC)

    # 1. Capture snapshot ids (before queries to document what was read)
    observed_snapshot_id: int | None = None
    current_snapshot_id: int | None = None
    try:
        artifacts_tbl = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
        snap = artifacts_tbl.current_snapshot()
        observed_snapshot_id = snap.snapshot_id if snap is not None else None
    except Exception:
        observed_snapshot_id = None

    try:
        facts_tbl = catalog.load_table(NORMALIZED_ACCESS_FACTS_TABLE)
        snap = facts_tbl.current_snapshot()
        current_snapshot_id = snap.snapshot_id if snap is not None else None
    except Exception:
        current_snapshot_id = None

    # 2. Create run row
    run = await create_run(
        session,
        application_id=application_id,
        observed_snapshot_id=observed_snapshot_id,
        current_snapshot_id=current_snapshot_id,
    )
    run_id = run.id

    try:
        # 3. Pipeline phases
        artifacts = _phase_load_artifacts(lake_session, application_id=application_id)
        artifacts_ingested = len(artifacts)

        raw_candidates, artifacts_unhandled = await _phase_dispatch(session, artifacts)
        current_keys, current_rows = _phase_load_current_state(lake_session, application_id=application_id)
        resolved_candidates, facts_errored = await _phase_resolve_action_ids(lake_session, raw_candidates)

        facts_created, facts_updated, facts_revoked, unchanged_count = await _phase_persist_delta(
            session,
            run_id=run_id,
            resolved_candidates=resolved_candidates,
            current_keys=current_keys,
            current_rows=current_rows,
            application_id=application_id,
            run_started_at=run_started_at,
        )

        # 4. Update run to pending_apply
        await update_run_status(
            session,
            run_id,
            status=ReconciliationRunStatus.pending_apply,
            counts=RunCounts(
                created=facts_created,
                updated=facts_updated,
                revoked=facts_revoked,
                unchanged=unchanged_count,
            ),
        )

    except Exception as exc:
        # 5. Mark run as failed; re-raise so caller can handle
        await update_run_status(
            session,
            run_id,
            status=ReconciliationRunStatus.failed,
            error=str(exc),
        )
        raise

    finished_at = datetime.now(UTC)

    return ReconciliationRunSummary(
        run_id=run_id,
        application_id=application_id,
        started_at=run_started_at,
        finished_at=finished_at,
        artifacts_ingested=artifacts_ingested,
        facts_created=facts_created,
        facts_updated=facts_updated,
        facts_revoked=facts_revoked,
        artifacts_unhandled=artifacts_unhandled,
        facts_errored=facts_errored,
        unchanged_count=unchanged_count,
        observed_snapshot_id=observed_snapshot_id,
        current_snapshot_id=current_snapshot_id,
    )
