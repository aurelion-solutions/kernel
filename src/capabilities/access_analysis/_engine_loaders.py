# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Bulk loaders for the four detection producers — private to the ScanEngine.

Each loader issues a fixed number of SQL round-trips regardless of result size (no N+1).
Loaders are read-only: no flush, no commit, no events.

Capability grants: per-subject (called once per subject in the SoD loop).
Rules + conditions + M2M: loaded once per scan run (shared across subjects).
Mitigations: loaded once per scan run as a flat list; caller filters per-subject.
Orphan / terminated / unused: single bulk SELECT per producer.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_grants.models import CapabilityGrant
from src.capabilities.access_analysis.data_access import iter_unused_access_fact_views
from src.capabilities.access_analysis.detectors.orphan import AccountView
from src.capabilities.access_analysis.detectors.terminated import (
    TERMINAL_STATUSES_BY_KIND,
    AccountWithSubjectView,
)
from src.capabilities.access_analysis.detectors.unused import AccessFactView
from src.capabilities.access_analysis.evaluators.repository import load_enabled_rules
from src.capabilities.access_analysis.evaluators.sod import (
    CapabilityGrantView,
    MitigationView,
    SodRuleView,
)
from src.capabilities.access_analysis.mitigations.models import Mitigation, MitigationStatus
from src.capabilities.effective_access.models import EffectiveGrant
from src.inventory.accounts.models import Account
from src.inventory.ownership_assignments.models import OwnershipAssignment
from src.inventory.subjects.models import Subject
from src.platform.lake.duckdb_session import LakeSession
from src.platform.logs.service import LogService

# ---------------------------------------------------------------------------
# SoD inputs
# ---------------------------------------------------------------------------


async def load_sod_rules(session: AsyncSession) -> list[SodRuleView]:
    """Load all enabled SodRules with conditions and M2M capability_ids. One query each."""
    return await load_enabled_rules(session)


async def load_all_mitigations(
    session: AsyncSession,
    at: datetime,
    scope_subject_id: UUID | None = None,
) -> list[MitigationView]:
    """Load all active/proposed mitigations valid at ``at`` in a single query.

    Caller filters per-subject before passing to the evaluator.
    Validity window: valid_from <= at AND (valid_until IS NULL OR valid_until > at).
    """
    stmt = (
        sa.select(Mitigation)
        .where(
            Mitigation.status.in_([MitigationStatus.active, MitigationStatus.proposed]),
            Mitigation.valid_from <= at,
            sa.or_(
                Mitigation.valid_until.is_(None),
                Mitigation.valid_until > at,
            ),
        )
        .order_by(Mitigation.subject_id, Mitigation.id)
    )
    if scope_subject_id is not None:
        stmt = stmt.where(Mitigation.subject_id == scope_subject_id)

    result = await session.execute(stmt)
    rows = result.scalars().all()

    return [
        MitigationView(
            id=m.id,
            rule_id=m.rule_id,
            subject_id=m.subject_id,
            scope_key_id=m.scope_key_id,
            scope_value=m.scope_value,
            status=m.status,
            valid_from=m.valid_from,
            valid_until=m.valid_until,
            created_at=m.created_at,
        )
        for m in rows
    ]


async def load_all_subject_ids(
    session: AsyncSession,
    scope_subject_id: UUID | None = None,
) -> list[UUID]:
    """Return all distinct subject_ids that have at least one CapabilityGrant.

    Narrowed to scope_subject_id when provided.
    """
    stmt = sa.select(CapabilityGrant.subject_id).distinct()
    if scope_subject_id is not None:
        stmt = stmt.where(CapabilityGrant.subject_id == scope_subject_id)
    result = await session.execute(stmt)
    return [row.subject_id for row in result.all()]


async def load_capability_grants_for_subject(
    session: AsyncSession,
    subject_id: UUID,
    at: datetime,
) -> list[CapabilityGrantView]:
    """Load active CapabilityGrants for one subject at point-in-time ``at``.

    Active-at predicate: observed_at <= at AND (tombstoned_at IS NULL OR tombstoned_at > at).
    Two queries: grants + capability slug, then EG source arrays.
    """
    grant_stmt = (
        sa.select(
            CapabilityGrant.id,
            CapabilityGrant.subject_id,
            CapabilityGrant.capability_id,
            Capability.slug.label('capability_slug'),
            CapabilityGrant.scope_key_id,
            CapabilityGrant.scope_value,
            CapabilityGrant.application_id,
            CapabilityGrant.source_effective_grant_id,
            CapabilityGrant.source_capability_mapping_id,
        )
        .join(Capability, Capability.id == CapabilityGrant.capability_id)
        .where(
            CapabilityGrant.subject_id == subject_id,
            CapabilityGrant.observed_at <= at,
            sa.or_(
                CapabilityGrant.tombstoned_at.is_(None),
                CapabilityGrant.tombstoned_at > at,
            ),
        )
        .order_by(CapabilityGrant.id)
    )
    grant_result = await session.execute(grant_stmt)
    grant_rows = grant_result.all()

    if not grant_rows:
        return []

    # Bulk-fetch EffectiveGrant source arrays
    eg_ids = {row.source_effective_grant_id for row in grant_rows}
    eg_stmt = sa.select(
        EffectiveGrant.id,
        EffectiveGrant.source_access_fact_id,
        EffectiveGrant.source_initiative_id,
    ).where(EffectiveGrant.id.in_(eg_ids))
    eg_result = await session.execute(eg_stmt)

    eg_map: dict[UUID, tuple[list[int], list[int]]] = {}
    for eg_row in eg_result.all():
        eg_id = eg_row.id
        if eg_id not in eg_map:
            eg_map[eg_id] = ([], [])
        fact_id_int = eg_row.source_access_fact_id.int if eg_row.source_access_fact_id else None
        init_id_int = eg_row.source_initiative_id.int if eg_row.source_initiative_id else None
        if fact_id_int is not None and fact_id_int not in eg_map[eg_id][0]:
            eg_map[eg_id][0].append(fact_id_int)
        if init_id_int is not None and init_id_int not in eg_map[eg_id][1]:
            eg_map[eg_id][1].append(init_id_int)

    views: list[CapabilityGrantView] = []
    for row in grant_rows:
        eg_id = row.source_effective_grant_id
        access_fact_ids, initiative_ids = eg_map.get(eg_id, ([], []))
        views.append(
            CapabilityGrantView(
                id=row.id,
                subject_id=row.subject_id,
                capability_id=row.capability_id,
                capability_slug=row.capability_slug,
                scope_key_id=row.scope_key_id,
                scope_value=row.scope_value,
                application_id=row.application_id,
                source_effective_grant_id=eg_id,
                source_capability_mapping_id=row.source_capability_mapping_id,
                source_access_fact_ids=sorted(access_fact_ids),
                source_initiative_ids=sorted(initiative_ids),
            )
        )
    return views


# ---------------------------------------------------------------------------
# Orphan inputs
# ---------------------------------------------------------------------------


async def load_orphan_inputs(
    session: AsyncSession,
    scope_application_id: UUID | None = None,
) -> list[AccountView]:
    """Load orphan account candidates (subject_id IS NULL).

    Uses DISTINCT ON subquery to find the most recent OwnershipAssignment per account_id.
    Single round-trip (one SELECT with subquery).
    """
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
    )

    if scope_application_id is not None:
        query = query.where(Account.application_id == scope_application_id)

    result = await session.execute(query)
    rows = result.all()

    return [
        AccountView(
            id=row.id,
            application_id=row.application_id,
            subject_id=row.subject_id,
            username=row.username,
            last_known_owner_subject_id=row.owner_subject_id,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Terminated inputs
# ---------------------------------------------------------------------------


async def load_terminated_inputs(
    session: AsyncSession,
    scope_subject_id: UUID | None = None,
    scope_application_id: UUID | None = None,
) -> list[AccountWithSubjectView]:
    """Load accounts whose linked subject is in a terminal status.

    INNER JOIN ent_accounts to subjects. Filters with TERMINAL_STATUSES_BY_KIND vocabulary.
    Single round-trip.
    """
    terminal_filter = sa.or_(
        *(sa.and_(Subject.kind == k, Subject.status.in_(statuses)) for k, statuses in TERMINAL_STATUSES_BY_KIND.items())
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
    )

    if scope_subject_id is not None:
        query = query.where(Account.subject_id == scope_subject_id)
    if scope_application_id is not None:
        query = query.where(Account.application_id == scope_application_id)

    result = await session.execute(query)
    rows = result.all()

    return [
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


# ---------------------------------------------------------------------------
# Unused inputs
# ---------------------------------------------------------------------------


async def load_unused_inputs(
    session: AsyncSession,
    lake_session: LakeSession,
    log_service: LogService,
    *,
    scope_subject_id: UUID | None = None,
    scope_application_id: UUID | None = None,
    batch_size: int = 1000,
    pg_any_array_max_size: int,
) -> list[AccessFactView]:
    """Load active access facts via DuckDB iceberg_scan + per-batch PG usage telemetry.

    Thin materialisation wrapper around ``iter_unused_access_fact_views``.
    ``ScanEngine`` passes the result list to ``detect_unused``; streaming is
    deferred to Step 16+ when ScanEngine itself becomes stream-aware.
    """
    views: list[AccessFactView] = []
    async for view in iter_unused_access_fact_views(
        lake_session=lake_session,
        pg_session=session,
        log_service=log_service,
        scope_application_id=scope_application_id,
        scope_subject_id=scope_subject_id,
        batch_size=batch_size,
        pg_any_array_max_size=pg_any_array_max_size,
    ):
        views.append(view)
    return views
