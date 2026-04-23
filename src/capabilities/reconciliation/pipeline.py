# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Artifact-first reconciliation pipeline.

Entry point: ``run_reconciliation(session, *, application_id, correlation_id)``.
Six private ``_phase_*`` helpers implement the algorithm.
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.reconciliation.contracts import NormalizationResult
from src.capabilities.reconciliation.registry import get_handler
from src.inventory.access_artifacts.models import AccessArtifact
from src.inventory.access_facts.models import AccessFact, AccessFactEffect
from src.inventory.artifact_bindings.service import ArtifactBindingDuplicateError, ArtifactBindingService
from src.inventory.resources.models import Resource

if TYPE_CHECKING:
    from src.capabilities.reconciliation.schemas import ReconciliationRunSummary
    from src.inventory.access_facts.service import AccessFactService

logger = logging.getLogger('reconciliation.pipeline')

# (subject_id | None, account_id | None, resource_id, action_id)
FactKey = tuple[UUID | None, UUID | None, UUID, int]

# Artifact + result pair — keeps artifact reference for binding write
_NormalizedCandidate = tuple[AccessArtifact, NormalizationResult]

# Resolved candidate: artifact, result, action_id (already resolved), fact_key
_ResolvedCandidate = tuple[AccessArtifact, NormalizationResult, int, FactKey]


# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------


async def _phase_load_artifacts(
    session: AsyncSession,
    application_id: UUID,
) -> list[AccessArtifact]:
    """Load all active AccessArtifact rows for the application."""
    result = await session.execute(
        select(AccessArtifact).where(
            AccessArtifact.application_id == application_id,
            AccessArtifact.is_active.is_(True),
        )
    )
    return list(result.scalars().all())


async def _phase_dispatch(
    session: AsyncSession,
    artifacts: list[AccessArtifact],
) -> tuple[list[_NormalizedCandidate], int]:
    """Dispatch each artifact to its registered handler.

    Returns (candidates, unhandled_count).
    """
    candidates: list[_NormalizedCandidate] = []
    unhandled = 0

    for artifact in artifacts:
        handler = get_handler(artifact.artifact_type)
        if handler is None:
            unhandled += 1
            continue
        try:
            results = await handler.handle(artifact, session)
        except Exception:
            # Per-artifact exception → log + skip; counted in facts_errored by caller
            logger.exception(
                'Handler error for artifact %s (type=%s)',
                artifact.id,
                artifact.artifact_type,
            )
            # We signal "errored" by yielding a sentinel: None result.
            # Caller tracks facts_errored separately; we use an explicit flag.
            candidates.append((artifact, None))  # type: ignore[arg-type]
            continue
        for result in results:
            candidates.append((artifact, result))

    return candidates, unhandled


async def _phase_load_current_state(
    session: AsyncSession,
    application_id: UUID,
) -> tuple[set[FactKey], dict[FactKey, AccessFact]]:
    """Load all ACTIVE AccessFact rows whose resource belongs to the application."""
    result = await session.execute(
        select(AccessFact)
        .join(Resource, AccessFact.resource_id == Resource.id)
        .where(
            Resource.application_id == application_id,
            AccessFact.is_active.is_(True),
        )
    )
    rows = list(result.scalars().all())
    current_keys: set[FactKey] = set()
    current_rows: dict[FactKey, AccessFact] = {}
    for row in rows:
        key: FactKey = (row.subject_id, row.account_id, row.resource_id, row.action_id)
        current_keys.add(key)
        current_rows[key] = row
    return current_keys, current_rows


async def _phase_resolve_action_ids(
    session: AsyncSession,
    candidates: list[_NormalizedCandidate],
) -> tuple[list[_ResolvedCandidate], int]:
    """Bulk-resolve action_slug → action_id.

    Returns (resolved_candidates, errored_count).
    Unknown slugs → errored_count incremented, candidate skipped.
    Errored candidates (handler exception, None result) → skipped + counted.
    """
    from src.inventory.actions.models import Action as RefAction

    # Collect unique slugs (skip errored / None results)
    slugs: set[str] = set()
    for _artifact, result in candidates:
        if result is not None:
            slugs.add(result.action_slug)

    # Bulk fetch
    slug_to_id: dict[str, int] = {}
    if slugs:
        rows = await session.execute(select(RefAction.id, RefAction.slug).where(RefAction.slug.in_(slugs)))
        for action_id, slug in rows:
            slug_to_id[slug] = action_id

    resolved: list[_ResolvedCandidate] = []
    errored = 0

    for artifact, result in candidates:
        if result is None:
            # Handler raised an exception — already logged; count as errored
            errored += 1
            continue
        action_id = slug_to_id.get(result.action_slug)
        if action_id is None:
            logger.warning(
                'Unknown action_slug %r for artifact %s — skipping',
                result.action_slug,
                artifact.id,
            )
            errored += 1
            continue
        key: FactKey = (result.subject_id, result.account_id, result.resource_id, action_id)
        resolved.append((artifact, result, action_id, key))

    return resolved, errored


async def _phase_apply_delta(
    session: AsyncSession,
    resolved_candidates: list[_ResolvedCandidate],
    current_keys: set[FactKey],
    current_rows: dict[FactKey, AccessFact],
    access_fact_service: AccessFactService,
    artifact_binding_service: ArtifactBindingService,
    run_started_at: datetime,
    correlation_id: str | None,
) -> tuple[int, int, int]:
    """Apply the set-diff and write facts + bindings.

    Returns (facts_created, facts_updated, facts_revoked).
    """
    # Build new key set and map key → (artifact, result)
    new_keys: set[FactKey] = set()
    key_to_candidate: dict[FactKey, tuple[AccessArtifact, NormalizationResult, int]] = {}

    for artifact, result, action_id, key in resolved_candidates:
        new_keys.add(key)
        key_to_candidate[key] = (artifact, result, action_id)

    created_keys = new_keys - current_keys
    revoked_keys = current_keys - new_keys
    common_keys = new_keys & current_keys

    facts_created = 0
    facts_updated = 0
    facts_revoked = 0

    # CREATE (or reactivate) facts
    for key in created_keys:
        artifact, result, _action_id = key_to_candidate[key]
        try:
            fact = await access_fact_service.create_fact(
                session,
                subject_id=result.subject_id or _require_subject_or_fail(result),
                account_id=result.account_id,
                resource_id=result.resource_id,
                action_slug=result.action_slug,
                effect=AccessFactEffect(result.effect),
                observed_at=run_started_at,
                valid_from=result.valid_from,
                valid_until=result.valid_until,
                correlation_id=correlation_id,
            )
        except Exception:
            logger.exception('Failed to create fact for artifact %s', artifact.id)
            continue

        facts_created += 1
        await _write_binding(session, artifact, fact.id, artifact_binding_service, correlation_id)

    # UPDATE (field drift)
    for key in common_keys:
        artifact, result, _action_id = key_to_candidate[key]
        current_fact = current_rows[key]

        # Compare mutable fields
        if isinstance(current_fact.effect, AccessFactEffect):
            current_effect_str = current_fact.effect.value
        else:
            current_effect_str = str(current_fact.effect)
        # valid_from is NOT NULL in schema; None from handler means "use existing"
        vf_differs = result.valid_from is not None and current_fact.valid_from != result.valid_from
        fields_differ = (
            current_effect_str != result.effect or vf_differs or current_fact.valid_until != result.valid_until
        )

        if not fields_differ:
            continue

        try:
            await access_fact_service.refresh_fact_fields(
                session,
                current_fact.id,
                effect=AccessFactEffect(result.effect),
                valid_from=result.valid_from,
                valid_until=result.valid_until,
                observed_at=run_started_at,
                correlation_id=correlation_id,
            )
        except Exception:
            logger.exception('Failed to refresh fact fields for artifact %s', artifact.id)
            continue

        facts_updated += 1
        await _write_binding(session, artifact, current_fact.id, artifact_binding_service, correlation_id)

    # REVOKE
    for key in revoked_keys:
        fact = current_rows[key]
        try:
            await access_fact_service.revoke_fact(
                session,
                fact.id,
                observed_at=run_started_at,
                correlation_id=correlation_id,
            )
        except Exception:
            logger.exception('Failed to revoke fact %s', fact.id)
            continue
        facts_revoked += 1

    return facts_created, facts_updated, facts_revoked


def _require_subject_or_fail(result: NormalizationResult) -> UUID:
    """Return subject_id; raise if None (both subject_id and account_id are None)."""
    if result.subject_id is None:
        raise ValueError('NormalizationResult must set at least one of subject_id or account_id')
    return result.subject_id


async def _write_binding(
    session: AsyncSession,
    artifact: AccessArtifact,
    fact_id: UUID,
    service: ArtifactBindingService,
    correlation_id: str | None,
) -> None:
    """Write ArtifactBinding; silently skip on duplicate."""
    try:
        await service.create_binding(
            session,
            artifact_id=artifact.id,
            target_type='access_fact',
            target_id=fact_id,
            correlation_id=correlation_id,
        )
    except ArtifactBindingDuplicateError:
        pass
    except Exception:
        logger.exception(
            'Failed to create ArtifactBinding for artifact %s → fact %s',
            artifact.id,
            fact_id,
        )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def run_reconciliation(
    session: AsyncSession,
    *,
    application_id: UUID,
    access_fact_service: AccessFactService,
    artifact_binding_service: ArtifactBindingService,
    correlation_id: str | None = None,
) -> ReconciliationRunSummary:
    """Run the artifact-first reconciliation pipeline for one application.

    Does NOT commit — caller (route or CLI) owns the transaction boundary.
    """
    from src.capabilities.reconciliation.schemas import ReconciliationRunSummary

    run_started_at = datetime.now(UTC)

    artifacts = await _phase_load_artifacts(session, application_id)
    artifacts_ingested = len(artifacts)

    raw_candidates, artifacts_unhandled = await _phase_dispatch(session, artifacts)
    current_keys, current_rows = await _phase_load_current_state(session, application_id)
    resolved_candidates, facts_errored = await _phase_resolve_action_ids(session, raw_candidates)

    facts_created, facts_updated, facts_revoked = await _phase_apply_delta(
        session,
        resolved_candidates,
        current_keys,
        current_rows,
        access_fact_service,
        artifact_binding_service,
        run_started_at,
        correlation_id,
    )

    finished_at = datetime.now(UTC)

    return ReconciliationRunSummary(
        application_id=application_id,
        started_at=run_started_at,
        finished_at=finished_at,
        artifacts_ingested=artifacts_ingested,
        facts_created=facts_created,
        facts_updated=facts_updated,
        facts_revoked=facts_revoked,
        artifacts_unhandled=artifacts_unhandled,
        facts_errored=facts_errored,
    )
