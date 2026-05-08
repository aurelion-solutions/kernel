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
from src.engines.access_analysis.data_access import iter_unused_access_fact_views
from src.engines.policy_assessment.policy_types.access_risk.evaluator import (
    AccountView,
    OrphanFinding,
    UnusedFinding,
    detect_orphans,
    detect_unused,
)
from src.engines.policy_assessment.policy_types.lifecycle.evaluator import (
    TERMINAL_STATUSES_BY_KIND,
    AccountWithSubjectView,
    TerminatedFinding,
    detect_terminated,
)
from src.inventory.accounts.models import Account
from src.inventory.ownership_assignments.models import OwnershipAssignment
from src.inventory.subjects.models import Subject
from src.platform.lake.duckdb_session import LakeSession
from src.platform.logs.service import LogService


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
    """Read-only service: loads active access facts via DuckDB iceberg_scan, calls pure detector.

    Reads ``normalized.access_facts`` via DuckDB + per-batch PG usage telemetry.
    No EventService — this is a read-only detection path.
    """

    def __init__(
        self,
        session: AsyncSession,
        lake_session: LakeSession,
        log_service: LogService,
        pg_any_array_max_size: int,
    ) -> None:
        self._session = session
        self._lake_session = lake_session
        self._log_service = log_service
        self._pg_any_array_max_size = pg_any_array_max_size

    async def run(
        self,
        application_id: UUID | None,
        threshold_days: int,
        limit: int,
    ) -> list[UnusedFinding]:
        """Detect active access facts with no recent (or no) usage telemetry.

        Reads from Iceberg ``normalized.access_facts`` via DuckDB. Per-batch PG
        query fetches MAX(last_seen) from ``access_usage_facts``. Stops collecting
        as soon as ``limit`` views are accumulated (early-break to reduce PG round-trips).

        Args:
            application_id: Optional filter to a single application's access facts.
            threshold_days: Minimum full days without usage to qualify as unused (>= 1).
            limit: Maximum number of candidate access facts to load (1–5000).

        Returns:
            Sorted list of UnusedFinding drafts (sorted by detect_unused internal key).
        """
        effective_batch = min(limit, 1000)

        views = []
        async for view in iter_unused_access_fact_views(
            lake_session=self._lake_session,
            pg_session=self._session,
            log_service=self._log_service,
            scope_application_id=application_id,
            scope_subject_id=None,
            batch_size=effective_batch,
            pg_any_array_max_size=self._pg_any_array_max_size,
        ):
            views.append(view)
            if len(views) >= limit:
                break

        return detect_unused(
            access_facts=views,
            threshold_days=threshold_days,
            at=datetime.now(tz=UTC),
        )
