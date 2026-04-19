# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for the EAS repository layer — 9 test cases.

Tests run against the real async session fixture (PostgreSQL).
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid
from uuid import UUID

import pytest
import sqlalchemy as sa
from src.capabilities.effective_access.models import EffectiveGrantEffect
from src.capabilities.effective_access.projector import EffectiveGrantDraft
from src.capabilities.effective_access.repository import (
    fetch_access_fact_with_initiatives,
    fetch_application_facts_with_initiatives,
    find_grants_for_access,
    get_effective_grant,
    list_effective_grants,
    tombstone_effective_grants_for_initiative,
    tombstone_effective_grants_for_missing_pairs,
    upsert_effective_grants,
)
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Seed helpers (copied shape from test_models.py)
# ---------------------------------------------------------------------------


async def _make_employee_subject(session) -> UUID:  # type: ignore[no-untyped-def]
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.subjects.models import Subject

    person = await create_person(session, external_id=str(uuid.uuid4()), description='test')
    await session.flush()
    emp = await create_employee(session, person_id=person.id)
    await session.flush()
    subj = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.employee,
        principal_employee_id=emp.id,
        status='active',
    )
    session.add(subj)
    await session.flush()
    return subj.id


async def _make_nhi_subject(session) -> UUID:  # type: ignore[no-untyped-def]
    from src.inventory.nhi.models import NHI
    from src.inventory.subjects.models import Subject, SubjectNHIKind

    nhi = NHI(
        external_id=str(uuid.uuid4()),
        name=f'test-nhi-{uuid.uuid4().hex[:8]}',
        kind='service_account',
        owner_employee_id=None,
    )
    session.add(nhi)
    await session.flush()
    subj = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status='active',
    )
    session.add(subj)
    await session.flush()
    return subj.id


async def _make_app_and_resource(session) -> tuple[UUID, UUID]:  # type: ignore[no-untyped-def]
    from src.inventory.resources.models import Resource
    from src.platform.applications.models import Application

    app = Application(
        name=f'test-app-{uuid.uuid4()}',
        code=f'app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    resource = Resource(
        external_id=str(uuid.uuid4()),
        application_id=app.id,
        kind='database',
    )
    session.add(resource)
    await session.flush()
    return app.id, resource.id


async def _make_access_fact(session, subject_id: UUID, resource_id: UUID) -> UUID:  # type: ignore[no-untyped-def]
    from src.inventory.access_facts.models import AccessFact, AccessFactEffect

    fact = AccessFact(
        subject_id=subject_id,
        resource_id=resource_id,
        action=Action.read,
        effect=AccessFactEffect.allow,
        valid_from=_NOW,
    )
    session.add(fact)
    await session.flush()
    return fact.id


async def _make_initiative(
    session, access_fact_id: UUID, *, valid_from: datetime = _NOW, valid_until: datetime | None = None
) -> UUID:  # type: ignore[no-untyped-def]
    from src.inventory.initiatives.models import Initiative

    init = Initiative(
        access_fact_id=access_fact_id,
        type=InitiativeType.birthright,
        origin='test-origin',
        valid_from=valid_from,
        valid_until=valid_until,
    )
    session.add(init)
    await session.flush()
    return init.id


def _draft(
    subject_id: UUID,
    subject_kind: SubjectKind,
    application_id: UUID,
    resource_id: UUID,
    source_access_fact_id: UUID,
    source_initiative_id: UUID,
    *,
    observed_at: datetime = _NOW,
    tombstoned_at: datetime | None = None,
) -> EffectiveGrantDraft:
    return EffectiveGrantDraft(
        subject_id=subject_id,
        subject_kind=subject_kind,
        application_id=application_id,
        account_id=None,
        resource_id=resource_id,
        action=Action.read,
        effect=EffectiveGrantEffect.allow,
        initiative_type=InitiativeType.birthright,
        initiative_origin='test-origin',
        valid_from=_NOW,
        valid_until=None,
        source_access_fact_id=source_access_fact_id,
        source_initiative_id=source_initiative_id,
        observed_at=observed_at,
        tombstoned_at=tombstoned_at,
    )


# ---------------------------------------------------------------------------
# Test 1 — fetch_access_fact_with_initiatives: None for unknown id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_access_fact_unknown_id_returns_none(session_factory) -> None:
    async with session_factory() as session:
        result = await fetch_access_fact_with_initiatives(session, uuid.uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# Test 2 — fetch_access_fact_with_initiatives: (row, []) for fact with no initiatives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_access_fact_no_initiatives(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        await session.commit()

    async with session_factory() as session:
        result = await fetch_access_fact_with_initiatives(session, fact_id)
        assert result is not None
        fact_row, initiatives = result
        assert fact_row.id == fact_id
        assert initiatives == []


# ---------------------------------------------------------------------------
# Test 3 — fetch_access_fact_with_initiatives: returns all N=3 initiatives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_access_fact_returns_all_three_initiatives(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_ids = {await _make_initiative(session, fact_id) for _ in range(3)}
        await session.commit()

    async with session_factory() as session:
        result = await fetch_access_fact_with_initiatives(session, fact_id)
        assert result is not None
        _, initiatives = result
        assert len(initiatives) == 3
        assert {i.id for i in initiatives} == init_ids


# ---------------------------------------------------------------------------
# Test 4 — fetch_access_fact_with_initiatives: pre-resolves subject_kind and application_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_access_fact_pre_resolves_subject_kind_and_app_id(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        await session.commit()

    async with session_factory() as session:
        result = await fetch_access_fact_with_initiatives(session, fact_id)
        assert result is not None
        fact_row, _ = result
        assert fact_row.subject_kind == SubjectKind.employee
        assert fact_row.application_id == app_id


# ---------------------------------------------------------------------------
# Test 5 — fetch_application_facts_with_initiatives: yields in batches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_application_facts_yields_in_batches(session_factory) -> None:
    """Seed 7 facts (each with unique resource); with batch_size=3 → 3 batches (3+3+1)."""
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        from src.inventory.access_facts.models import AccessFact, AccessFactEffect
        from src.inventory.resources.models import Resource
        from src.platform.applications.models import Application

        app = Application(
            name=f'test-app-batch-{uuid.uuid4()}',
            code=f'ba-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.flush()
        app_id = app.id

        for _ in range(7):
            resource = Resource(
                external_id=str(uuid.uuid4()),
                application_id=app_id,
                kind='database',
            )
            session.add(resource)
            await session.flush()
            fact = AccessFact(
                subject_id=subject_id,
                resource_id=resource.id,
                action=Action.read,
                effect=AccessFactEffect.allow,
                valid_from=_NOW,
            )
            session.add(fact)
        await session.commit()

    async with session_factory() as session:
        yielded = []
        async for fact_row, _ in fetch_application_facts_with_initiatives(session, app_id, batch_size=3):
            yielded.append(fact_row.id)

        assert len(yielded) == 7
        # All ids unique
        assert len(set(yielded)) == 7


# ---------------------------------------------------------------------------
# Test 6 — fetch_application_facts_with_initiatives: scopes correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_application_facts_scopes_correctly(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id_1, resource_id_1 = await _make_app_and_resource(session)
        app_id_2, resource_id_2 = await _make_app_and_resource(session)

        fact1_id = await _make_access_fact(session, subject_id, resource_id_1)
        fact2_id = await _make_access_fact(session, subject_id, resource_id_2)
        await session.commit()

    async with session_factory() as session:
        yielded = []
        async for fact_row, _ in fetch_application_facts_with_initiatives(session, app_id_1):
            yielded.append(fact_row.id)

        assert fact1_id in yielded
        assert fact2_id not in yielded


# ---------------------------------------------------------------------------
# Test 7 — upsert_effective_grants: inserts N drafts on clean table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_effective_grants_insert_new_rows(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_id_1 = await _make_initiative(session, fact_id)
        init_id_2 = await _make_initiative(session, fact_id)

        drafts = [
            _draft(subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id_1),
            _draft(subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id_2),
        ]

        result = await upsert_effective_grants(session, drafts)
        assert result.rows_upserted == 2
        assert result.rows_inserted == 2
        assert result.rows_updated == 0
        assert result.rows_tombstoned == 0


# ---------------------------------------------------------------------------
# Test 8 — upsert_effective_grants: updates in-place (same id survives)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_effective_grants_updates_in_place(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_id = await _make_initiative(session, fact_id)

        draft1 = _draft(
            subject_id,
            SubjectKind.employee,
            app_id,
            resource_id,
            fact_id,
            init_id,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        r1 = await upsert_effective_grants(session, [draft1])
        await session.flush()

        # Capture the id that was assigned on first insert
        row = await session.execute(
            sa.text(
                'SELECT id FROM effective_grants WHERE source_access_fact_id = :fid AND source_initiative_id = :iid'
            ),
            {'fid': fact_id, 'iid': init_id},
        )
        first_id = row.scalar_one()

        # Upsert again with advanced observed_at
        draft2 = _draft(
            subject_id,
            SubjectKind.employee,
            app_id,
            resource_id,
            fact_id,
            init_id,
            observed_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        r2 = await upsert_effective_grants(session, [draft2])
        await session.flush()

        assert r1.rows_inserted == 1
        assert r2.rows_inserted == 0
        assert r2.rows_updated == 1

        # id must be the same after update
        row2 = await session.execute(
            sa.text(
                'SELECT id FROM effective_grants WHERE source_access_fact_id = :fid AND source_initiative_id = :iid'
            ),
            {'fid': fact_id, 'iid': init_id},
        )
        second_id = row2.scalar_one()
        assert first_id == second_id


# ---------------------------------------------------------------------------
# Test 9 — upsert_effective_grants: mixed new + existing — correct split counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_effective_grants_mixed_split_counts(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_id_existing = await _make_initiative(session, fact_id)
        init_id_new = await _make_initiative(session, fact_id)

        # Pre-insert one row
        existing_draft = _draft(subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id_existing)
        await upsert_effective_grants(session, [existing_draft])
        await session.flush()

        # Now upsert both — one will be update, one will be insert
        drafts = [
            _draft(subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id_existing),
            _draft(subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id_new),
        ]
        result = await upsert_effective_grants(session, drafts)

        assert result.rows_upserted == 2
        assert result.rows_inserted == 1
        assert result.rows_updated == 1


# ---------------------------------------------------------------------------
# Step 4 read helper tests (R1–R9)
# ---------------------------------------------------------------------------


async def _seed_grant(
    session,  # type: ignore[no-untyped-def]
    subject_id: UUID,
    subject_kind: SubjectKind,
    application_id: UUID,
    resource_id: UUID,
    source_access_fact_id: UUID,
    source_initiative_id: UUID,
    *,
    effect: EffectiveGrantEffect = EffectiveGrantEffect.allow,
    observed_at: datetime = _NOW,
    tombstoned_at: datetime | None = None,
    valid_until: datetime | None = None,
) -> UUID:
    """Insert one EffectiveGrant and flush; returns its id."""
    d = _draft(
        subject_id,
        subject_kind,
        application_id,
        resource_id,
        source_access_fact_id,
        source_initiative_id,
        observed_at=observed_at,
        tombstoned_at=tombstoned_at,
    )
    # Override effect and valid_until via upsert helper
    from src.capabilities.effective_access.projector import EffectiveGrantDraft

    full_draft = EffectiveGrantDraft(
        subject_id=d.subject_id,
        subject_kind=d.subject_kind,
        application_id=d.application_id,
        account_id=d.account_id,
        resource_id=d.resource_id,
        action=d.action,
        effect=effect,
        initiative_type=d.initiative_type,
        initiative_origin=d.initiative_origin,
        valid_from=d.valid_from,
        valid_until=valid_until,
        source_access_fact_id=d.source_access_fact_id,
        source_initiative_id=d.source_initiative_id,
        observed_at=observed_at,
        tombstoned_at=tombstoned_at,
    )
    await upsert_effective_grants(session, [full_draft])
    await session.flush()
    # Return the generated id
    row = await session.execute(
        sa.text('SELECT id FROM effective_grants WHERE source_access_fact_id = :fid AND source_initiative_id = :iid'),
        {'fid': source_access_fact_id, 'iid': source_initiative_id},
    )
    return row.scalar_one()


# ---------------------------------------------------------------------------
# R1 — list by subject_id, ORDER BY observed_at DESC verified
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_effective_grants_by_subject_id(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        other_subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        fact_other = await _make_access_fact(session, other_subject_id, resource_id)
        init_id_1 = await _make_initiative(session, fact_id)
        init_id_2 = await _make_initiative(session, fact_id)
        init_id_other = await _make_initiative(session, fact_other)

        t1 = datetime(2026, 1, 1, tzinfo=UTC)
        t2 = datetime(2026, 1, 2, tzinfo=UTC)
        t3 = datetime(2026, 1, 3, tzinfo=UTC)

        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id_1, observed_at=t1
        )
        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id_2, observed_at=t3
        )
        await _seed_grant(
            session,
            other_subject_id,
            SubjectKind.employee,
            app_id,
            resource_id,
            fact_other,
            init_id_other,
            observed_at=t2,
        )
        await session.commit()

    async with session_factory() as session:
        rows = await list_effective_grants(session, subject_id=subject_id, active_only=False)
        assert all(r.subject_id == subject_id for r in rows)
        assert len(rows) == 2
        # ORDER BY observed_at DESC
        assert rows[0].observed_at >= rows[1].observed_at


# ---------------------------------------------------------------------------
# R2 — list by resource_id + action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_effective_grants_by_resource_and_action(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        _, resource_id_2 = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        fact_id_2 = await _make_access_fact(session, subject_id, resource_id_2)
        init_id = await _make_initiative(session, fact_id)
        init_id_2 = await _make_initiative(session, fact_id_2)

        await _seed_grant(session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id)
        await _seed_grant(session, subject_id, SubjectKind.employee, app_id, resource_id_2, fact_id_2, init_id_2)
        await session.commit()

    async with session_factory() as session:
        rows = await list_effective_grants(session, resource_id=resource_id, action=Action.read, active_only=False)
        assert all(r.resource_id == resource_id for r in rows)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# R3 — list by source_initiative_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_effective_grants_by_source_initiative_id(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_a_1 = await _make_initiative(session, fact_id)
        init_a_2 = await _make_initiative(session, fact_id)
        init_a_3 = await _make_initiative(session, fact_id)
        init_b_1 = await _make_initiative(session, fact_id)
        init_b_2 = await _make_initiative(session, fact_id)
        init_b_3 = await _make_initiative(session, fact_id)

        # Initiative group A (3 rows)
        for init_id in (init_a_1, init_a_2, init_a_3):
            await _seed_grant(session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id)
        # Initiative group B (3 rows)
        for init_id in (init_b_1, init_b_2, init_b_3):
            await _seed_grant(session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id)
        await session.commit()

    async with session_factory() as session:
        rows_a = await list_effective_grants(session, source_initiative_id=init_a_1, active_only=False)
        assert len(rows_a) == 1
        assert rows_a[0].source_initiative_id == init_a_1


# ---------------------------------------------------------------------------
# R4 — active_only predicate (tombstoned / valid_until expired / valid_until IS NULL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_effective_grants_active_only_predicate(session_factory) -> None:
    t_now = datetime(2026, 6, 1, tzinfo=UTC)
    t_past = datetime(2026, 1, 1, tzinfo=UTC)  # valid_until <= t_now → excluded

    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_tombstoned = await _make_initiative(session, fact_id)
        init_expired = await _make_initiative(session, fact_id)
        init_open = await _make_initiative(session, fact_id)

        # Row 1: tombstoned
        await _seed_grant(
            session,
            subject_id,
            SubjectKind.employee,
            app_id,
            resource_id,
            fact_id,
            init_tombstoned,
            tombstoned_at=t_past,
        )
        # Row 2: valid_until in the past
        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_expired, valid_until=t_past
        )
        # Row 3: valid_until IS NULL (open-ended)
        await _seed_grant(session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_open)
        await session.commit()

    async with session_factory() as session:
        active_rows = await list_effective_grants(session, subject_id=subject_id, active_only=True, now=t_now)
        # Only the open-ended row should survive
        assert len(active_rows) == 1
        assert active_rows[0].source_initiative_id == init_open


# ---------------------------------------------------------------------------
# R5 — active_only=False returns tombstoned rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_effective_grants_active_only_false(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_live = await _make_initiative(session, fact_id)
        init_dead = await _make_initiative(session, fact_id)

        await _seed_grant(session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_live)
        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_dead, tombstoned_at=_NOW
        )
        await session.commit()

    async with session_factory() as session:
        rows = await list_effective_grants(session, subject_id=subject_id, active_only=False)
        assert len(rows) == 2
        tombstoned = [r for r in rows if r.tombstoned_at is not None]
        assert len(tombstoned) == 1


# ---------------------------------------------------------------------------
# R6 — pagination: 11 rows, limit=5 pages correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_effective_grants_pagination(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)

        for i in range(11):
            init_id = await _make_initiative(session, fact_id)
            ts = datetime(2026, 1, i + 1, tzinfo=UTC)
            await _seed_grant(
                session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id, observed_at=ts
            )
        await session.commit()

    async with session_factory() as session:
        page1 = await list_effective_grants(session, subject_id=subject_id, active_only=False, limit=5, offset=0)
        page2 = await list_effective_grants(session, subject_id=subject_id, active_only=False, limit=5, offset=5)
        page3 = await list_effective_grants(session, subject_id=subject_id, active_only=False, limit=5, offset=10)

        assert len(page1) == 5
        assert len(page2) == 5
        assert len(page3) == 1

        # Pages are disjoint
        ids1 = {r.id for r in page1}
        ids2 = {r.id for r in page2}
        ids3 = {r.id for r in page3}
        assert ids1.isdisjoint(ids2)
        assert ids1.isdisjoint(ids3)
        assert ids2.isdisjoint(ids3)

        # Ordering is stable across calls (newest first)
        all_rows = page1 + page2 + page3
        for i in range(len(all_rows) - 1):
            assert all_rows[i].observed_at >= all_rows[i + 1].observed_at


# ---------------------------------------------------------------------------
# R7 — get_effective_grant: hit across partitions (employee and nhi)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_effective_grant_hit_across_partitions(session_factory) -> None:
    async with session_factory() as session:
        emp_subject_id = await _make_employee_subject(session)
        nhi_subject_id = await _make_nhi_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_emp = await _make_access_fact(session, emp_subject_id, resource_id)
        fact_nhi = await _make_access_fact(session, nhi_subject_id, resource_id)
        init_emp = await _make_initiative(session, fact_emp)
        init_nhi = await _make_initiative(session, fact_nhi)

        emp_id = await _seed_grant(
            session, emp_subject_id, SubjectKind.employee, app_id, resource_id, fact_emp, init_emp
        )
        nhi_id = await _seed_grant(session, nhi_subject_id, SubjectKind.nhi, app_id, resource_id, fact_nhi, init_nhi)
        await session.commit()

    async with session_factory() as session:
        emp_row = await get_effective_grant(session, emp_id)
        assert emp_row is not None
        assert emp_row.id == emp_id
        assert emp_row.subject_kind == SubjectKind.employee

        nhi_row = await get_effective_grant(session, nhi_id)
        assert nhi_row is not None
        assert nhi_row.id == nhi_id
        assert nhi_row.subject_kind == SubjectKind.nhi


# ---------------------------------------------------------------------------
# R8 — get_effective_grant: miss returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_effective_grant_miss_returns_none(session_factory) -> None:
    async with session_factory() as session:
        row = await get_effective_grant(session, uuid.uuid4())
        assert row is None


# ---------------------------------------------------------------------------
# R9 — find_grants_for_access: fan-out + active filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_grants_for_access_fan_out_and_active_filter(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)

        init_1 = await _make_initiative(session, fact_id)
        init_2 = await _make_initiative(session, fact_id)
        init_3 = await _make_initiative(session, fact_id)

        await _seed_grant(session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_1)
        await _seed_grant(session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_2)
        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_3, tombstoned_at=_NOW
        )
        await session.commit()

    async with session_factory() as session:
        # All three rows (active_only=False)
        all_rows = await find_grants_for_access(
            session, subject_id=subject_id, resource_id=resource_id, action=Action.read, active_only=False
        )
        assert len(all_rows) == 3

        # Only two live rows (active_only=True)
        active_rows = await find_grants_for_access(
            session, subject_id=subject_id, resource_id=resource_id, action=Action.read, active_only=True, now=_NOW
        )
        assert len(active_rows) == 2
        assert all(r.tombstoned_at is None for r in active_rows)


# ---------------------------------------------------------------------------
# Constraint name pin test (§9.12 risk 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uq_effective_grants_source_pair_constraint_exists(session_factory) -> None:
    """Verify the named constraint exists in pg_constraint — catches rename drift."""
    async with session_factory() as session:
        result = await session.execute(
            sa.text("SELECT conname FROM pg_constraint WHERE conname = 'uq_effective_grants_source_pair'")
        )
        row = result.scalar_one_or_none()
        assert row == 'uq_effective_grants_source_pair', (
            "Constraint 'uq_effective_grants_source_pair' not found in pg_constraint — "
            'repository ON CONFLICT target will be broken'
        )


# ---------------------------------------------------------------------------
# R-I1 — tombstone_effective_grants_for_initiative: isolation guarantee (Step 6a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_by_initiative_tombstones_only_matching_rows(session_factory) -> None:
    """R-I1: only the row for the expired initiative is tombstoned; the other row is untouched."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = datetime(2026, 1, 2, tzinfo=UTC)

    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_a = await _make_initiative(session, fact_id)
        init_b = await _make_initiative(session, fact_id)

        # Seed both rows at T0
        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_a, observed_at=t0
        )
        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_b, observed_at=t0
        )
        await session.flush()

        rowcount = await tombstone_effective_grants_for_initiative(session, initiative_id=init_a, observed_at=t1)
        assert rowcount == 1

        row_a = await session.execute(
            sa.text('SELECT tombstoned_at, observed_at FROM effective_grants WHERE source_initiative_id = :iid'),
            {'iid': init_a},
        )
        g_a = row_a.one()
        assert g_a.tombstoned_at == t1
        assert g_a.observed_at == t1

        row_b = await session.execute(
            sa.text('SELECT tombstoned_at, observed_at FROM effective_grants WHERE source_initiative_id = :iid'),
            {'iid': init_b},
        )
        g_b = row_b.one()
        assert g_b.tombstoned_at is None
        assert g_b.observed_at == t0

        await session.rollback()


# ---------------------------------------------------------------------------
# R-I2 — tombstone_effective_grants_for_initiative: idempotent and order-safe (Step 6a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_by_initiative_is_idempotent_and_order_safe(session_factory) -> None:
    """R-I2: double-CAS guard — same-timestamp is no-op; stale is no-op; newer re-touches."""
    t_neg1 = datetime(2025, 12, 31, tzinfo=UTC)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = datetime(2026, 1, 2, tzinfo=UTC)

    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_id = await _make_initiative(session, fact_id)

        # Seed at T0
        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id, observed_at=t0
        )
        await session.flush()

        # First tombstone at T0 — succeeds
        rc1 = await tombstone_effective_grants_for_initiative(session, initiative_id=init_id, observed_at=t0)
        # CAS: observed_at < t0 is false for the row (row.observed_at == t0), so no update
        assert rc1 == 0

        # Seed fresh with observed_at=t0
        # Let's reset the row so observed_at < t0 never holds; instead seed at t_neg1
        await session.rollback()

    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_id = await _make_initiative(session, fact_id)

        # Seed at t_neg1 so CAS allows t0 and t1 to win
        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id, observed_at=t_neg1
        )
        await session.flush()

        # Tombstone at T0 — row.observed_at=t_neg1 < t0, so succeeds
        rc1 = await tombstone_effective_grants_for_initiative(session, initiative_id=init_id, observed_at=t0)
        assert rc1 == 1

        # Same T0 again — row.observed_at is now t0, CAS observed_at < t0 fails → no-op
        rc2 = await tombstone_effective_grants_for_initiative(session, initiative_id=init_id, observed_at=t0)
        assert rc2 == 0

        # Stale T_-1 < T0 — CAS observed_at < t_neg1 fails → no-op
        rc3 = await tombstone_effective_grants_for_initiative(session, initiative_id=init_id, observed_at=t_neg1)
        assert rc3 == 0

        # T1 > T0 — row.observed_at=t0 < t1, CAS allows; tombstoned_at=t0 > t1 is False, but IS NULL check
        # Row has tombstoned_at=t0. Condition: tombstoned_at IS NULL OR tombstoned_at > t1 → t0 > t1 is False,
        # IS NULL is False → row is not touched. Correct: once tombstoned at t0, only a later t1 can re-touch
        # if tombstoned_at > t1. Here t0 < t1, so tombstoned_at (t0) > t1 is False → no-op.
        rc4 = await tombstone_effective_grants_for_initiative(session, initiative_id=init_id, observed_at=t1)
        assert rc4 == 0

        row = await session.execute(
            sa.text('SELECT tombstoned_at, observed_at FROM effective_grants WHERE source_initiative_id = :iid'),
            {'iid': init_id},
        )
        g = row.one()
        assert g.tombstoned_at == t0
        assert g.observed_at == t0

        await session.rollback()


# ---------------------------------------------------------------------------
# R-D1 — tombstone_effective_grants_for_missing_pairs: only disappeared rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_missing_pairs_tombstones_only_disappeared(session_factory) -> None:
    """R-D1: helper tombstones only the initiative absent from the live set."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = datetime(2026, 1, 2, tzinfo=UTC)

    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_a = await _make_initiative(session, fact_id)
        init_b = await _make_initiative(session, fact_id)

        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_a, observed_at=t0
        )
        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_b, observed_at=t0
        )
        await session.flush()

        rowcount = await tombstone_effective_grants_for_missing_pairs(
            session,
            access_fact_id=fact_id,
            observed_at=t1,
            live_initiative_ids={init_a},
        )
        assert rowcount == 1

        # Init A grant: untouched
        row_a = await session.execute(
            sa.text('SELECT tombstoned_at, observed_at FROM effective_grants WHERE source_initiative_id = :iid'),
            {'iid': init_a},
        )
        g_a = row_a.one()
        assert g_a.tombstoned_at is None
        assert g_a.observed_at == t0

        # Init B grant: tombstoned at t1
        row_b = await session.execute(
            sa.text('SELECT tombstoned_at, observed_at FROM effective_grants WHERE source_initiative_id = :iid'),
            {'iid': init_b},
        )
        g_b = row_b.one()
        assert g_b.tombstoned_at == t1
        assert g_b.observed_at == t1

        await session.rollback()


# ---------------------------------------------------------------------------
# R-D2 — tombstone_effective_grants_for_missing_pairs: empty live set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_missing_pairs_empty_live_set_tombstones_all(session_factory) -> None:
    """R-D2: empty live_initiative_ids tombstones all grants for the fact.

    Anti-regression guard against SQLAlchemy notin_([]) quirk — the helper
    must NOT become a no-op when the allowlist is empty.
    """
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = datetime(2026, 1, 2, tzinfo=UTC)

    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_a = await _make_initiative(session, fact_id)
        init_b = await _make_initiative(session, fact_id)

        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_a, observed_at=t0
        )
        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_b, observed_at=t0
        )
        await session.flush()

        rowcount = await tombstone_effective_grants_for_missing_pairs(
            session,
            access_fact_id=fact_id,
            observed_at=t1,
            live_initiative_ids=set(),  # explicit empty — anti-regression seam
        )
        assert rowcount == 2

        tomb_count = await session.execute(
            sa.text('SELECT COUNT(*) FROM effective_grants WHERE source_access_fact_id = :fid AND tombstoned_at = :t1'),
            {'fid': fact_id, 't1': t1},
        )
        assert tomb_count.scalar_one() == 2

        await session.rollback()


# ---------------------------------------------------------------------------
# R-D3 — tombstone_effective_grants_for_missing_pairs: CAS guard rejects stale
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_missing_pairs_respects_cas_guard(session_factory) -> None:
    """R-D3: stale observed_at (t1 < row.observed_at=t2) is rejected by double CAS guard."""
    t1 = datetime(2026, 1, 2, tzinfo=UTC)
    t2 = datetime(2026, 1, 3, tzinfo=UTC)

    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_id = await _make_initiative(session, fact_id)

        # Seed at t2 — "fresh" row
        await _seed_grant(
            session, subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id, observed_at=t2
        )
        await session.flush()

        # Attempt stale tombstone at t1 < t2 with init_id NOT in live set
        rowcount = await tombstone_effective_grants_for_missing_pairs(
            session,
            access_fact_id=fact_id,
            observed_at=t1,
            live_initiative_ids=set(),  # init_id is "missing"
        )
        assert rowcount == 0

        row = await session.execute(
            sa.text('SELECT tombstoned_at, observed_at FROM effective_grants WHERE source_initiative_id = :iid'),
            {'iid': init_id},
        )
        g = row.one()
        assert g.tombstoned_at is None
        assert g.observed_at == t2

        await session.rollback()
