# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Finding deduplication and mitigation-relink helpers — private to the ScanEngine.

Dedup key: 7 columns — kind (NOT NULL), subject_id (NULLABLE), account_id (NULLABLE),
rule_id (NULLABLE), scope_key_id (NULLABLE), scope_value (NULLABLE), evidence_hash (NOT NULL).

The pre-SELECT uses IS NOT DISTINCT FROM for the 5 nullable columns and = for kind + evidence_hash,
matching the uq_findings_evidence UNIQUE constraint which has NULLS NOT DISTINCT semantics.

Concurrency note: under concurrent scans, the pre-SELECT can race with another writer's insert.
Mitigation: catch IntegrityError on insert, treat as a late hit, re-SELECT, fall through to
the relink branch. This step's POST /scan-runs/{id}/run is synchronous — races only occur
across overlapping runs of different ScanRun rows.

No events emitted here. Only service.py emits events.
No session.commit() calls here. Callers own transaction boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.assessment.findings.models import Finding, FindingKind, FindingStatus
from src.inventory.policy.sod_rules.models import SodSeverity

# ---------------------------------------------------------------------------
# DTOs returned from dedupe helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FindingEmission:
    """Minimal finding data needed by service.py to build events.

    Avoids reaching back into ORM rows for event emission.
    """

    finding_id: int
    kind: FindingKind
    severity: SodSeverity
    subject_id: UUID | None
    account_id: UUID | None
    rule_id: int | None
    scope_key_id: int | None
    scope_value: str | None
    evidence_hash: str
    scan_run_id: int


@dataclass(frozen=True)
class StatusChangeEmission:
    """One status flip (open → mitigated) observed during mitigation relinking."""

    finding_id: int
    from_status: FindingStatus
    to_status: FindingStatus
    status_reason: str
    active_mitigation_id: int


# ---------------------------------------------------------------------------
# Dedup select — IS NOT DISTINCT FROM for nullable columns
# ---------------------------------------------------------------------------

_DEDUP_SELECT = sa.text(
    """
    SELECT id, status, active_mitigation_id, proposed_mitigation_id
      FROM findings
     WHERE kind           = :kind
       AND subject_id     IS NOT DISTINCT FROM :subject_id
       AND account_id     IS NOT DISTINCT FROM :account_id
       AND rule_id        IS NOT DISTINCT FROM :rule_id
       AND scope_key_id   IS NOT DISTINCT FROM :scope_key_id
       AND scope_value    IS NOT DISTINCT FROM :scope_value
       AND evidence_hash  = :evidence_hash
    """
)


async def _select_existing(
    session: AsyncSession,
    *,
    kind: FindingKind,
    subject_id: UUID | None,
    account_id: UUID | None,
    rule_id: int | None,
    scope_key_id: int | None,
    scope_value: str | None,
    evidence_hash: str,
) -> tuple[int, FindingStatus, int | None, int | None] | None:
    """Return (id, status, active_mitigation_id, proposed_mitigation_id) or None."""
    result = await session.execute(
        _DEDUP_SELECT,
        {
            'kind': kind.value,
            'subject_id': str(subject_id) if subject_id is not None else None,
            'account_id': str(account_id) if account_id is not None else None,
            'rule_id': rule_id,
            'scope_key_id': scope_key_id,
            'scope_value': scope_value,
            'evidence_hash': evidence_hash,
        },
    )
    row = result.fetchone()
    if row is None:
        return None
    return row.id, FindingStatus(row.status), row.active_mitigation_id, row.proposed_mitigation_id


# ---------------------------------------------------------------------------
# Mitigation relinking — open → mitigated when active_mitigation_id appears
# ---------------------------------------------------------------------------


def _needs_relink(
    current_status: FindingStatus,
    current_active_mitigation_id: int | None,
    new_active_mitigation_id: int | None,
    current_proposed_mitigation_id: int | None,
    new_proposed_mitigation_id: int | None,
) -> bool:
    """Return True if the persisted row needs any mitigation-related update."""
    if current_status == FindingStatus.open and new_active_mitigation_id is not None:
        return True
    if current_active_mitigation_id != new_active_mitigation_id:
        return True
    if current_proposed_mitigation_id != new_proposed_mitigation_id:
        return True
    return False


async def _apply_relink(
    session: AsyncSession,
    finding: Finding,
    *,
    current_status: FindingStatus,
    new_active_mitigation_id: int | None,
    new_proposed_mitigation_id: int | None,
) -> StatusChangeEmission | None:
    """Apply mitigation linkage changes to the finding row. Return StatusChangeEmission if status flipped."""
    status_change: StatusChangeEmission | None = None

    if current_status == FindingStatus.open and new_active_mitigation_id is not None:
        # open → mitigated
        finding.status = FindingStatus.mitigated
        finding.status_changed_at = datetime.now(UTC)
        finding.status_reason = 'mitigation_activated'
        status_change = StatusChangeEmission(
            finding_id=finding.id,
            from_status=FindingStatus.open,
            to_status=FindingStatus.mitigated,
            status_reason='mitigation_activated',
            active_mitigation_id=new_active_mitigation_id,
        )

    finding.active_mitigation_id = new_active_mitigation_id
    finding.proposed_mitigation_id = new_proposed_mitigation_id
    await session.flush()
    return status_change


# ---------------------------------------------------------------------------
# Public dedupe + persist function
# ---------------------------------------------------------------------------


async def upsert_finding(
    session: AsyncSession,
    *,
    scan_run_id: int,
    kind: FindingKind,
    subject_id: UUID | None,
    account_id: UUID | None,
    rule_id: int | None,
    scope_key_id: int | None,
    scope_value: str | None,
    evidence_hash: str,
    severity: SodSeverity,
    evaluated_at: datetime,
    matched_capability_grant_ids: list[int],
    matched_effective_grant_ids: list[str],
    matched_access_fact_ids: list[str],
    active_mitigation_id: int | None,
    proposed_mitigation_id: int | None,
) -> tuple[FindingEmission, bool, StatusChangeEmission | None]:
    """Deduplicate and persist one finding.

    Returns:
        (emission, is_new, status_change_emission)
        - is_new=True  → inserted (findings_created_count += 1)
        - is_new=False → reused  (findings_reused_count += 1)
        - status_change_emission → non-None if status flipped open→mitigated

    Algorithm:
        1. Pre-SELECT using IS NOT DISTINCT FROM for nullable columns.
        2. On hit → optionally relink mitigation fields.
        3. On miss → INSERT; if IntegrityError (race) re-SELECT and fall through to relink.
    """
    # Step 1: pre-SELECT
    existing = await _select_existing(
        session,
        kind=kind,
        subject_id=subject_id,
        account_id=account_id,
        rule_id=rule_id,
        scope_key_id=scope_key_id,
        scope_value=scope_value,
        evidence_hash=evidence_hash,
    )

    if existing is None:
        # Step 3: attempt INSERT
        new_finding = Finding(
            scan_run_id=scan_run_id,
            kind=kind,
            subject_id=subject_id,
            account_id=account_id,
            rule_id=rule_id,
            scope_key_id=scope_key_id,
            scope_value=scope_value,
            severity=severity,
            status=FindingStatus.open,
            matched_capability_grant_ids=matched_capability_grant_ids,
            matched_effective_grant_ids=matched_effective_grant_ids,
            matched_access_fact_ids=matched_access_fact_ids,
            evidence_hash=evidence_hash,
            active_mitigation_id=active_mitigation_id,
            proposed_mitigation_id=proposed_mitigation_id,
            detected_at=evaluated_at,
            evaluated_at=evaluated_at,
        )
        session.add(new_finding)
        try:
            await session.flush()
        except IntegrityError:
            # Concurrent insert race — fall through to relink branch below
            await session.rollback()
            existing = await _select_existing(
                session,
                kind=kind,
                subject_id=subject_id,
                account_id=account_id,
                rule_id=rule_id,
                scope_key_id=scope_key_id,
                scope_value=scope_value,
                evidence_hash=evidence_hash,
            )
            if existing is None:
                # Shouldn't happen after IntegrityError, but guard defensively
                raise  # re-raise original IntegrityError
        else:
            # Successful insert
            emission = FindingEmission(
                finding_id=new_finding.id,
                kind=kind,
                severity=severity,
                subject_id=subject_id,
                account_id=account_id,
                rule_id=rule_id,
                scope_key_id=scope_key_id,
                scope_value=scope_value,
                evidence_hash=evidence_hash,
                scan_run_id=scan_run_id,
            )
            return emission, True, None

    # existing row found (original hit or post-race re-select)
    ex_id, ex_status, ex_active_mit, ex_proposed_mit = existing

    # Step 2: relink if needed
    status_change: StatusChangeEmission | None = None
    if _needs_relink(ex_status, ex_active_mit, active_mitigation_id, ex_proposed_mit, proposed_mitigation_id):
        finding_row = await session.get(Finding, ex_id)
        if finding_row is not None:
            status_change = await _apply_relink(
                session,
                finding_row,
                current_status=ex_status,
                new_active_mitigation_id=active_mitigation_id,
                new_proposed_mitigation_id=proposed_mitigation_id,
            )

    emission = FindingEmission(
        finding_id=ex_id,
        kind=kind,
        severity=severity,
        subject_id=subject_id,
        account_id=account_id,
        rule_id=rule_id,
        scope_key_id=scope_key_id,
        scope_value=scope_value,
        evidence_hash=evidence_hash,
        scan_run_id=scan_run_id,
    )
    return emission, False, status_change
