# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Finding repository — plain async functions over AsyncSession.

No commits. Service flushes; caller commits (per ARCH_CONTEXT transaction-ownership rule).

No insert helper is exposed here — findings are written by the engine (Step 14).
"""

from __future__ import annotations

from datetime import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.findings.models import Finding, FindingKind, FindingStatus
from src.capabilities.access_analysis.mitigations.models import Mitigation
from src.capabilities.access_analysis.sod_rules.models import SodSeverity


async def get_finding_by_id(
    session: AsyncSession,
    finding_id: int,
) -> Finding | None:
    """Return the Finding with the given id, or None."""
    stmt = sa.select(Finding).where(Finding.id == finding_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_findings(
    session: AsyncSession,
    *,
    scan_run_id: int | None = None,
    rule_id: int | None = None,
    severity: SodSeverity | None = None,
    status: FindingStatus | None = None,
    kind: FindingKind | None = None,
    subject_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Finding]:
    """Return Findings ordered by id DESC, optionally filtered."""
    stmt = sa.select(Finding).order_by(Finding.id.desc())
    if scan_run_id is not None:
        stmt = stmt.where(Finding.scan_run_id == scan_run_id)
    if rule_id is not None:
        stmt = stmt.where(Finding.rule_id == rule_id)
    if severity is not None:
        stmt = stmt.where(Finding.severity == severity)
    if status is not None:
        stmt = stmt.where(Finding.status == status)
    if kind is not None:
        stmt = stmt.where(Finding.kind == kind)
    if subject_id is not None:
        stmt = stmt.where(Finding.subject_id == subject_id)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_finding_status_fields(
    session: AsyncSession,
    finding: Finding,
    *,
    status: FindingStatus,
    status_changed_at: datetime,
    status_reason: str | None,
    active_mitigation_id: int | None = None,
) -> Finding:
    """Update status fields (and optionally active_mitigation_id), flush, return refreshed entity."""
    finding.status = status
    finding.status_changed_at = status_changed_at
    finding.status_reason = status_reason
    if active_mitigation_id is not None:
        finding.active_mitigation_id = active_mitigation_id
    await session.flush()
    await session.refresh(finding)
    return finding


async def get_mitigation_for_linkage(
    session: AsyncSession,
    mitigation_id: int,
) -> Mitigation | None:
    """Return the Mitigation row needed for linkage validation, or None if not found.

    Cross-slice ORM read — consistent with the engine's bulk-load pattern.
    Does NOT import or call MitigationService; no circular dependency.
    """
    stmt = sa.select(Mitigation).where(Mitigation.id == mitigation_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
