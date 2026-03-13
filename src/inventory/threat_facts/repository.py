# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ThreatFact repository for PostgreSQL access."""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.threat_facts.models import ThreatFact


async def get_threat_fact_by_id(
    session: AsyncSession,
    fact_id: uuid.UUID,
) -> ThreatFact | None:
    """Load threat fact by id."""
    result = await session.execute(select(ThreatFact).where(ThreatFact.id == fact_id))
    return result.scalar_one_or_none()


async def get_threat_fact_by_subject_id(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> ThreatFact | None:
    """Load threat fact by subject_id (at most one row per subject)."""
    result = await session.execute(select(ThreatFact).where(ThreatFact.subject_id == subject_id))
    return result.scalar_one_or_none()


async def list_threat_facts(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    min_risk_score: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ThreatFact]:
    """List threat facts with optional filters, ordered by observed_at DESC."""
    query = select(ThreatFact).order_by(ThreatFact.observed_at.desc())

    if subject_id is not None:
        query = query.where(ThreatFact.subject_id == subject_id)
    if account_id is not None:
        query = query.where(ThreatFact.account_id == account_id)
    if min_risk_score is not None:
        query = query.where(ThreatFact.risk_score >= min_risk_score)

    query = query.limit(min(limit, 200)).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def upsert_threat_fact(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    account_id: uuid.UUID | None,
    risk_score: float,
    active_indicators: list[str],
    last_login_at: datetime | None,
    failed_auth_count: int,
    observed_at: datetime,
) -> tuple[ThreatFact, bool]:
    """Upsert a threat fact keyed on subject_id.

    Returns (fact, created) where created=True if a new row was inserted.
    Uses read-then-write; concurrent PUT races are caught at the service layer.
    """
    existing = await get_threat_fact_by_subject_id(session, subject_id)
    if existing is None:
        fact = ThreatFact(
            subject_id=subject_id,
            account_id=account_id,
            risk_score=risk_score,
            active_indicators=active_indicators,
            last_login_at=last_login_at,
            failed_auth_count=failed_auth_count,
            observed_at=observed_at,
        )
        session.add(fact)
        await session.flush()
        await session.refresh(fact)
        return fact, True

    existing.account_id = account_id
    existing.risk_score = risk_score
    existing.active_indicators = active_indicators
    existing.last_login_at = last_login_at
    existing.failed_auth_count = failed_auth_count
    existing.observed_at = observed_at
    await session.flush()
    await session.refresh(existing)
    return existing, False
