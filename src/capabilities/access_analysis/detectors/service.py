# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Detector services — read-only wrappers around pure detector functions.

Never calls session.flush(), session.commit(), or emits events.
The route handler owns the transaction (per ARCH_CONTEXT "Transaction ownership").
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.detectors.orphan import AccountView, OrphanFinding, detect_orphans
from src.capabilities.access_analysis.detectors.terminated import (
    TERMINAL_STATUSES_BY_KIND,
    AccountWithSubjectView,
    TerminatedFinding,
    detect_terminated,
)
from src.capabilities.access_analysis.detectors.unused import AccessFactView, UnusedFinding, detect_unused
from src.inventory.access_facts.models import AccessFact
from src.inventory.access_usage_facts.models import AccessUsageFact
from src.inventory.accounts.models import Account
from src.inventory.ownership_assignments.models import OwnershipAssignment
from src.inventory.resources.models import Resource
from src.inventory.subjects.models import Subject


class OrphanDetectorService:
    """Read-only service: loads orphan Account candidates, surfaces last known owner, calls pure detector.

    No LogService, no EventService — this is a read-only detection path.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run(
        self,
        application_id: UUID | None,
        limit: int,
    ) -> list[OrphanFinding]:
        """Detect orphan accounts (subject_id IS NULL) within optional application scope.

        Steps:
          1. Build a DISTINCT ON subquery to find the most recent OwnershipAssignment
             per account_id (filtered to account_id IS NOT NULL, ordered by created_at DESC).
          2. LEFT JOIN Account rows (subject_id IS NULL) with the subquery.
          3. Apply optional application_id filter and LIMIT.
          4. Construct AccountView DTOs and call pure detect_orphans().
          5. Return the list of OrphanFinding drafts. Never persists. Never emits.

        Args:
            application_id: Optional filter to a single application's accounts.
            limit: Maximum number of candidate accounts to load (1–5000).

        Returns:
            Sorted list of OrphanFinding drafts (sorted by detect_orphans internal key).
        """
        # DISTINCT ON subquery: most recent ownership assignment per account_id
        # Filter account_id IS NOT NULL (resource-side rows are irrelevant)
        oa_subq = (
            sa.select(
                OwnershipAssignment.account_id,
                OwnershipAssignment.subject_id.label('owner_subject_id'),
            )
            .where(OwnershipAssignment.account_id.is_not(None))
            .distinct(OwnershipAssignment.account_id)
            .order_by(
                OwnershipAssignment.account_id,
                OwnershipAssignment.created_at.desc(),
            )
            .subquery('latest_owner')
        )

        # Main query: orphan accounts LEFT JOINed with latest owner
        query = (
            sa.select(
                Account.id,
                Account.application_id,
                Account.username,
                Account.subject_id,
                oa_subq.c.owner_subject_id,
            )
            .outerjoin(oa_subq, oa_subq.c.account_id == Account.id)
            .where(Account.subject_id.is_(None))
            .limit(limit)
        )

        if application_id is not None:
            query = query.where(Account.application_id == application_id)

        result = await self._session.execute(query)
        rows = result.all()

        account_views = [
            AccountView(
                id=row.id,
                application_id=row.application_id,
                subject_id=row.subject_id,
                username=row.username,
                last_known_owner_subject_id=row.owner_subject_id,
            )
            for row in rows
        ]

        return detect_orphans(accounts=account_views, at=datetime.now(tz=UTC))


class TerminatedDetectorService:
    """Read-only service: loads Account+Subject joined rows filtered to terminal statuses, calls pure detector.

    No LogService, no EventService — this is a read-only detection path.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run(
        self,
        application_id: UUID | None,
        limit: int,
    ) -> list[TerminatedFinding]:
        """Detect accounts whose linked Subject is in a terminal status for its kind.

        Steps:
          1. INNER JOIN ent_accounts to subjects on accounts.subject_id = subjects.id.
          2. Filter with a WHERE clause generated from TERMINAL_STATUSES_BY_KIND
             (one OR-clause per (kind, status_set) pair) — single source of truth.
          3. Apply optional application_id filter and LIMIT.
          4. Construct AccountWithSubjectView DTOs and call pure detect_terminated().
          5. Return the list of TerminatedFinding drafts. Never persists. Never emits.

        Args:
            application_id: Optional filter to a single application's accounts.
            limit: Maximum number of candidate accounts to load (1–5000).

        Returns:
            Sorted list of TerminatedFinding drafts (sorted by detect_terminated internal key).
        """
        terminal_filter = sa.or_(
            *(
                sa.and_(Subject.kind == k, Subject.status.in_(statuses))
                for k, statuses in TERMINAL_STATUSES_BY_KIND.items()
            )
        )

        query = (
            sa.select(
                Account.id,
                Account.application_id,
                Account.username,
                Account.subject_id,
                Subject.kind.label('subject_kind'),
                Subject.status.label('subject_status'),
                Subject.external_id.label('subject_external_id'),
            )
            .join(Subject, Account.subject_id == Subject.id)
            .where(terminal_filter)
            .limit(limit)
        )

        if application_id is not None:
            query = query.where(Account.application_id == application_id)

        result = await self._session.execute(query)
        rows = result.all()

        account_views = [
            AccountWithSubjectView(
                id=row.id,
                application_id=row.application_id,
                subject_id=row.subject_id,
                username=row.username,
                subject_kind=row.subject_kind,
                subject_status=row.subject_status,
                subject_external_id=row.subject_external_id,
            )
            for row in rows
        ]

        return detect_terminated(accounts=account_views, at=datetime.now(tz=UTC))


class UnusedDetectorService:
    """Read-only service: loads active AccessFact rows joined to usage telemetry, calls pure detector.

    No LogService, no EventService — this is a read-only detection path.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run(
        self,
        application_id: UUID | None,
        threshold_days: int,
        limit: int,
    ) -> list[UnusedFinding]:
        """Detect active access facts with no recent (or no) usage telemetry.

        Steps:
          1. Build a GROUP BY subquery over access_usage_facts returning
             (access_fact_id, MAX(last_seen) AS last_seen) — the usage aggregate.
          2. LEFT JOIN access_facts to the aggregate subquery.
          3. INNER JOIN access_facts to resources to obtain application_id.
          4. Filter access_facts.is_active = true.
          5. Apply optional application_id filter on resources.application_id and LIMIT.
          6. Construct AccessFactView DTOs and call detect_unused() with at=datetime.now(UTC).
          7. Return the list of UnusedFinding drafts. Never persists. Never emits.

        Args:
            application_id: Optional filter to a single application's access facts.
            threshold_days: Minimum full days without usage to qualify as unused (>= 1).
            limit: Maximum number of candidate access facts to load (1–5000).

        Returns:
            Sorted list of UnusedFinding drafts (sorted by detect_unused internal key).
        """
        # Subquery: MAX(last_seen) per access_fact_id
        usage_subq = (
            sa.select(
                AccessUsageFact.access_fact_id,
                sa.func.max(AccessUsageFact.last_seen).label('last_seen'),
            )
            .group_by(AccessUsageFact.access_fact_id)
            .subquery('usage_agg')
        )

        # Main query: active access facts LEFT JOIN usage aggregate, INNER JOIN resource
        query = (
            sa.select(
                AccessFact.id,
                AccessFact.subject_id,
                AccessFact.account_id,
                AccessFact.resource_id,
                AccessFact.valid_from,
                Resource.application_id,
                usage_subq.c.last_seen,
            )
            .join(Resource, AccessFact.resource_id == Resource.id)
            .outerjoin(usage_subq, usage_subq.c.access_fact_id == AccessFact.id)
            .where(AccessFact.is_active.is_(True))
            .limit(limit)
        )

        if application_id is not None:
            query = query.where(Resource.application_id == application_id)

        result = await self._session.execute(query)
        rows = result.all()

        access_fact_views = [
            AccessFactView(
                id=row.id,
                subject_id=row.subject_id,
                account_id=row.account_id,
                resource_id=row.resource_id,
                application_id=row.application_id,
                valid_from=row.valid_from,
                last_seen=row.last_seen,
            )
            for row in rows
        ]

        return detect_unused(
            access_facts=access_fact_views,
            threshold_days=threshold_days,
            at=datetime.now(tz=UTC),
        )
