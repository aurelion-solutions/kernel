# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for EffectiveAccessProjectionService — 8 test cases.

Uses a slice-local CapturingLogService duck-typed fake (~20 lines).
Does NOT reuse CapturingLogSink (wrong layer) or any platform log test double.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
import uuid
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from src.capabilities.effective_access.models import EffectiveGrantEffect
from src.capabilities.effective_access.projector import EffectiveGrantDraft
from src.capabilities.effective_access.repository import upsert_effective_grants
from src.capabilities.effective_access.schemas import IncrementalApplyKind, ProjectionScopeKind
from src.capabilities.effective_access.service import (
    EffectiveAccessProjectionService,
    EffectiveAccessReadService,
)
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind
from src.platform.logs.schemas import LogLevel

_NOW = datetime(2026, 1, 1, tzinfo=UTC)

# ---------------------------------------------------------------------------
# CapturingLogService — slice-local duck-typed fake (~20 lines)
# ---------------------------------------------------------------------------


class CapturingLogService:
    """Minimal fake that captures every emit_safe call as a plain tuple."""

    def __init__(self) -> None:
        self.events: list[tuple[str, LogLevel, str, str, dict[str, Any], dict[str, Any]]] = []

    def emit_safe(
        self,
        event_type: str,
        level: LogLevel,
        message: str,
        component: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        self.events.append((event_type, level, message, component, payload, kwargs))

    def emit_log(
        self,
        event_type: str,
        level: LogLevel,
        message: str,
        component: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        self.events.append((event_type, level, message, component, payload, kwargs))

    def emit_event_safe(self, event: Any) -> None:
        pass

    def emit_event(self, event: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Seed helpers
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


async def _make_access_fact(  # type: ignore[no-untyped-def]
    session,
    subject_id: UUID,
    resource_id: UUID,
    *,
    effect: str = 'allow',
    valid_from: datetime = _NOW,
    valid_until: datetime | None = None,
) -> UUID:
    from src.inventory.access_facts.models import AccessFact, AccessFactEffect

    fact = AccessFact(
        subject_id=subject_id,
        resource_id=resource_id,
        action=Action.read,
        effect=AccessFactEffect(effect),
        valid_from=valid_from,
        valid_until=valid_until,
    )
    session.add(fact)
    await session.flush()
    return fact.id


async def _make_initiative(  # type: ignore[no-untyped-def]
    session,
    access_fact_id: UUID,
    *,
    valid_from: datetime = _NOW,
    valid_until: datetime | None = None,
) -> UUID:
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


# ---------------------------------------------------------------------------
# Test 1 — happy path: one fact + one ALLOW initiative → one row, correct fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_access_fact_happy_path(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        await _make_initiative(session, fact_id)

        log = CapturingLogService()
        svc = EffectiveAccessProjectionService(session, log)  # type: ignore[arg-type]
        summary = await svc.project_access_fact(access_fact_id=fact_id, now=_NOW)

        assert summary.rows_inserted == 1
        assert summary.rows_updated == 0
        assert summary.rows_tombstoned == 0
        assert summary.pairs_projected == 1

        row = await session.execute(
            sa.text('SELECT tombstoned_at, effect FROM effective_grants WHERE source_access_fact_id = :fid'),
            {'fid': fact_id},
        )
        grant = row.one()
        assert grant.tombstoned_at is None
        assert grant.effect == 'allow'

        await session.rollback()


# ---------------------------------------------------------------------------
# Test 2 — idempotency: second call returns rows_inserted=0, same ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_access_fact_idempotency(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        await _make_initiative(session, fact_id)
        await session.commit()

    async with session_factory() as session:
        log = CapturingLogService()
        svc = EffectiveAccessProjectionService(session, log)  # type: ignore[arg-type]
        s1 = await svc.project_access_fact(access_fact_id=fact_id, now=_NOW)
        await session.commit()

    async with session_factory() as session:
        ids_after_first = set(
            (
                await session.execute(
                    sa.text('SELECT id FROM effective_grants WHERE source_access_fact_id = :fid'),
                    {'fid': fact_id},
                )
            )
            .scalars()
            .all()
        )

        log2 = CapturingLogService()
        svc2 = EffectiveAccessProjectionService(session, log2)  # type: ignore[arg-type]
        s2 = await svc2.project_access_fact(access_fact_id=fact_id, now=_NOW)
        await session.flush()

        ids_after_second = set(
            (
                await session.execute(
                    sa.text('SELECT id FROM effective_grants WHERE source_access_fact_id = :fid'),
                    {'fid': fact_id},
                )
            )
            .scalars()
            .all()
        )

        assert s2.rows_inserted == 0
        assert s2.rows_updated == s1.rows_upserted
        assert ids_after_first == ids_after_second
        await session.rollback()


# ---------------------------------------------------------------------------
# Test 3 — DENY fact → row has effect = deny
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_access_fact_deny(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id, effect='deny')
        await _make_initiative(session, fact_id)

        log = CapturingLogService()
        svc = EffectiveAccessProjectionService(session, log)  # type: ignore[arg-type]
        await svc.project_access_fact(access_fact_id=fact_id, now=_NOW)

        row = await session.execute(
            sa.text('SELECT effect FROM effective_grants WHERE source_access_fact_id = :fid'),
            {'fid': fact_id},
        )
        assert row.scalar_one() == 'deny'
        await session.rollback()


# ---------------------------------------------------------------------------
# Test 4 — birth-tombstone: fact valid_from=T2, initiative valid_until=T1, T1<T2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_access_fact_birth_tombstone(session_factory) -> None:
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 1, tzinfo=UTC)  # T2 > T1

    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        # fact valid_from = T2
        fact_id = await _make_access_fact(session, subject_id, resource_id, valid_from=t2)
        # initiative valid_until = T1 (before fact starts)
        await _make_initiative(session, fact_id, valid_from=t1, valid_until=t1)

        now = datetime(2026, 7, 1, tzinfo=UTC)
        log = CapturingLogService()
        svc = EffectiveAccessProjectionService(session, log)  # type: ignore[arg-type]
        summary = await svc.project_access_fact(access_fact_id=fact_id, now=now)

        assert summary.rows_tombstoned == 1

        row = await session.execute(
            sa.text('SELECT tombstoned_at FROM effective_grants WHERE source_access_fact_id = :fid'),
            {'fid': fact_id},
        )
        tombstoned_at = row.scalar_one()
        assert tombstoned_at is not None
        await session.rollback()


# ---------------------------------------------------------------------------
# Test 5 — event emission: exactly one eas.projection.completed, payload shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_access_fact_event_emission(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        await _make_initiative(session, fact_id)

        log = CapturingLogService()
        svc = EffectiveAccessProjectionService(session, log)  # type: ignore[arg-type]
        summary = await svc.project_access_fact(access_fact_id=fact_id, now=_NOW)

        assert len(log.events) == 1
        event_type, level, message, component, payload, kwargs = log.events[0]
        assert event_type == 'eas.projection.completed'
        assert level == LogLevel.INFO
        assert component == 'effective_access'
        assert payload['mode'] == 'batch'
        assert payload['change_kind'] is None
        assert payload['scope_kind'] == 'access_fact'
        assert payload['scope_id'] == str(fact_id)
        assert payload['rows_upserted'] == 1
        assert payload['rows_inserted'] == 1
        assert payload['rows_tombstoned'] == 0
        assert payload['rows_skipped'] == 0
        assert payload['triggered_by'] == 'api'
        assert str(summary.correlation_id) == kwargs.get('correlation_id')

        await session.rollback()


# ---------------------------------------------------------------------------
# Test 6 — no event on exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_access_fact_no_event_on_exception(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        await _make_initiative(session, fact_id)

        log = CapturingLogService()
        svc = EffectiveAccessProjectionService(session, log)  # type: ignore[arg-type]

        with patch(
            'src.capabilities.effective_access.service.upsert_effective_grants',
            new=AsyncMock(side_effect=IntegrityError('boom', None, Exception())),
        ):
            with pytest.raises(IntegrityError):
                await svc.project_access_fact(access_fact_id=fact_id, now=_NOW)

        # Only eas.projection.failed may appear (from projector ValueError path),
        # but NOT eas.projection.completed — in this case no events at all
        completed_events = [e for e in log.events if e[0] == 'eas.projection.completed']
        assert completed_events == []
        await session.rollback()


# ---------------------------------------------------------------------------
# Test 7 — project_application happy path: 3 facts → 3 rows, one event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_application_happy_path(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        from src.inventory.resources.models import Resource
        from src.platform.applications.models import Application

        app = Application(
            name=f'test-app-{uuid.uuid4()}',
            code=f'ap-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.flush()
        app_id = app.id

        for _ in range(3):
            resource = Resource(
                external_id=str(uuid.uuid4()),
                application_id=app_id,
                kind='database',
            )
            session.add(resource)
            await session.flush()
            fid = await _make_access_fact(session, subject_id, resource.id)
            await _make_initiative(session, fid)

        log = CapturingLogService()
        svc = EffectiveAccessProjectionService(session, log)  # type: ignore[arg-type]
        summary = await svc.project_application(application_id=app_id, now=_NOW)

        assert summary.rows_upserted == 3
        assert summary.rows_inserted == 3
        assert summary.scope_kind == ProjectionScopeKind.APPLICATION

        assert len(log.events) == 1
        event_type, _, _, _, payload, _ = log.events[0]
        assert event_type == 'eas.projection.completed'
        assert payload['rows_upserted'] == 3

        await session.rollback()


# ---------------------------------------------------------------------------
# Test 8 — cross-kind partition routing via tableoid::regclass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_application_cross_kind_partition_routing(session_factory) -> None:
    async with session_factory() as session:
        emp_subject_id = await _make_employee_subject(session)
        nhi_subject_id = await _make_nhi_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)

        emp_fact_id = await _make_access_fact(session, emp_subject_id, resource_id)
        await _make_initiative(session, emp_fact_id)

        nhi_fact_id = await _make_access_fact(session, nhi_subject_id, resource_id)
        await _make_initiative(session, nhi_fact_id)

        log = CapturingLogService()
        svc = EffectiveAccessProjectionService(session, log)  # type: ignore[arg-type]
        summary = await svc.project_application(application_id=app_id, now=_NOW)
        await session.flush()

        assert summary.rows_upserted == 2

        # Verify partition routing via subject_kind column
        emp_rows = await session.execute(
            sa.text('SELECT id FROM effective_grants_employee WHERE source_access_fact_id = :fid'),
            {'fid': emp_fact_id},
        )
        assert emp_rows.scalar_one_or_none() is not None

        nhi_rows = await session.execute(
            sa.text('SELECT id FROM effective_grants_nhi WHERE source_access_fact_id = :fid'),
            {'fid': nhi_fact_id},
        )
        assert nhi_rows.scalar_one_or_none() is not None

        # Employee row must NOT appear in nhi partition
        emp_in_nhi = await session.execute(
            sa.text('SELECT id FROM effective_grants_nhi WHERE source_access_fact_id = :fid'),
            {'fid': emp_fact_id},
        )
        assert emp_in_nhi.scalar_one_or_none() is None

        await session.rollback()


# ---------------------------------------------------------------------------
# Step 4 — EffectiveAccessReadService tests (S1–S5)
# ---------------------------------------------------------------------------


def _make_draft(
    subject_id: UUID,
    subject_kind: SubjectKind,
    app_id: UUID,
    resource_id: UUID,
    fact_id: UUID,
    init_id: UUID,
    *,
    effect: str = 'allow',
    observed_at: datetime = _NOW,
    tombstoned_at: datetime | None = None,
    valid_until: datetime | None = None,
) -> EffectiveGrantDraft:
    return EffectiveGrantDraft(
        subject_id=subject_id,
        subject_kind=subject_kind,
        application_id=app_id,
        account_id=None,
        resource_id=resource_id,
        action=Action.read,
        effect=EffectiveGrantEffect(effect),
        initiative_type=InitiativeType.birthright,
        initiative_origin='test-origin',
        valid_from=_NOW,
        valid_until=valid_until,
        source_access_fact_id=fact_id,
        source_initiative_id=init_id,
        observed_at=observed_at,
        tombstoned_at=tombstoned_at,
    )


# ---------------------------------------------------------------------------
# S1 — list_grants delegates to repository with filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_service_list_grants(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_a = await _make_initiative(session, fact_id)
        init_b = await _make_initiative(session, fact_id)

        d_a = _make_draft(subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_a)
        d_b = _make_draft(subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_b)
        await upsert_effective_grants(session, [d_a])
        await upsert_effective_grants(session, [d_b])
        await session.flush()

        svc = EffectiveAccessReadService(session)
        rows = await svc.list_grants(source_initiative_id=init_a, active_only=False)
        assert len(rows) == 1
        assert rows[0].source_initiative_id == init_a

        await session.rollback()


# ---------------------------------------------------------------------------
# S2 — get_grant: hit and miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_service_get_grant(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_id = await _make_initiative(session, fact_id)

        d = _make_draft(subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id)
        await upsert_effective_grants(session, [d])
        await session.flush()

        row = await session.execute(
            sa.text('SELECT id FROM effective_grants WHERE source_initiative_id = :iid'),
            {'iid': init_id},
        )
        grant_id = row.scalar_one()

        svc = EffectiveAccessReadService(session)

        # Hit
        found = await svc.get_grant(grant_id)
        assert found is not None
        assert found.id == grant_id

        # Miss
        missing = await svc.get_grant(uuid.uuid4())
        assert missing is None

        await session.rollback()


# ---------------------------------------------------------------------------
# S3 — explain_access: all allow → effect='allow'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_service_explain_access_all_allow(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_1 = await _make_initiative(session, fact_id)
        init_2 = await _make_initiative(session, fact_id)

        for init_id in (init_1, init_2):
            d = _make_draft(subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id, effect='allow')
            await upsert_effective_grants(session, [d])
        await session.flush()

        svc = EffectiveAccessReadService(session)
        result = await svc.explain_access(
            subject_id=subject_id,
            resource_id=resource_id,
            action=Action.read,
            active_only=False,
        )
        assert result.effect == 'allow'
        assert len(result.grants) == 2

        await session.rollback()


# ---------------------------------------------------------------------------
# S4 — explain_access: one deny → effect='deny' (deny-wins)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_service_explain_access_deny_wins(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        init_allow_1 = await _make_initiative(session, fact_id)
        init_allow_2 = await _make_initiative(session, fact_id)
        init_deny = await _make_initiative(session, fact_id)

        for init_id in (init_allow_1, init_allow_2):
            d = _make_draft(subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_id, effect='allow')
            await upsert_effective_grants(session, [d])
        d_deny = _make_draft(subject_id, SubjectKind.employee, app_id, resource_id, fact_id, init_deny, effect='deny')
        await upsert_effective_grants(session, [d_deny])
        await session.flush()

        svc = EffectiveAccessReadService(session)
        result = await svc.explain_access(
            subject_id=subject_id,
            resource_id=resource_id,
            action=Action.read,
            active_only=False,
        )
        assert result.effect == 'deny'

        await session.rollback()


# ---------------------------------------------------------------------------
# S5 — explain_access: zero matches → effect='none', grants=[]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_service_explain_access_none(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        _, resource_id = await _make_app_and_resource(session)

        svc = EffectiveAccessReadService(session)
        result = await svc.explain_access(
            subject_id=subject_id,
            resource_id=resource_id,
            action=Action.read,
        )
        assert result.effect == 'none'
        assert result.grants == []

        await session.rollback()


# ---------------------------------------------------------------------------
# TestIncrementalApply — T1–T5 (Phase 09 Step 5)
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = datetime(2026, 1, 2, tzinfo=UTC)


class TestIncrementalApply:
    """Tests for EffectiveAccessProjectionService.apply_incremental_change (Step 5 + 6a)."""

    @pytest.mark.asyncio
    async def test_apply_upsert_creates_new_rows(self, session_factory) -> None:
        """T1: upsert creates a new row with the correct observed_at and emits incremental event."""
        async with session_factory() as session:
            subject_id = await _make_employee_subject(session)
            app_id, resource_id = await _make_app_and_resource(session)
            fact_id = await _make_access_fact(session, subject_id, resource_id)
            await _make_initiative(session, fact_id)

            log = CapturingLogService()
            svc = EffectiveAccessProjectionService(session, log)  # type: ignore[arg-type]
            summary = await svc.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T0,
            )

            assert summary.rows_inserted == 1
            assert summary.rows_updated == 0
            assert summary.rows_skipped == 0
            assert summary.rows_tombstoned == 0
            assert summary.pairs_projected == 1

            row = await session.execute(
                sa.text('SELECT observed_at, tombstoned_at FROM effective_grants WHERE source_access_fact_id = :fid'),
                {'fid': fact_id},
            )
            grant = row.one()
            assert grant.tombstoned_at is None
            assert grant.observed_at == _T0

            assert len(log.events) == 1
            event_type, level, _, _, payload, _ = log.events[0]
            assert event_type == 'eas.projection.completed'
            assert level == LogLevel.INFO
            assert payload['mode'] == 'incremental'
            assert payload['change_kind'] == 'upsert'
            assert payload['rows_inserted'] == 1
            assert payload['rows_skipped'] == 0
            assert payload['triggered_by'] == 'consumer'
            assert 'causation_event_id' not in payload

            await session.rollback()

    @pytest.mark.asyncio
    async def test_apply_upsert_updates_when_observed_at_newer(self, session_factory) -> None:
        """T2: second upsert at T1 > T0 updates the existing row."""
        async with session_factory() as session:
            subject_id = await _make_employee_subject(session)
            app_id, resource_id = await _make_app_and_resource(session)
            fact_id = await _make_access_fact(session, subject_id, resource_id)
            await _make_initiative(session, fact_id)
            await session.commit()

        async with session_factory() as session:
            log1 = CapturingLogService()
            svc1 = EffectiveAccessProjectionService(session, log1)  # type: ignore[arg-type]
            await svc1.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T0,
            )
            await session.commit()

        async with session_factory() as session:
            # Flip the fact's effect via raw UPDATE to produce an observable diff
            # without emitting extra inventory events through the service.
            await session.execute(
                sa.text("UPDATE access_facts SET effect = 'deny' WHERE id = :fid"),
                {'fid': fact_id},
            )

            log2 = CapturingLogService()
            svc2 = EffectiveAccessProjectionService(session, log2)  # type: ignore[arg-type]
            summary = await svc2.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T1,
            )

            assert summary.rows_updated == 1
            assert summary.rows_inserted == 0
            assert summary.rows_skipped == 0

            row = await session.execute(
                sa.text('SELECT observed_at, effect FROM effective_grants WHERE source_access_fact_id = :fid'),
                {'fid': fact_id},
            )
            grant = row.one()
            assert grant.observed_at == _T1
            assert grant.effect == 'deny'

            await session.rollback()

    @pytest.mark.asyncio
    async def test_apply_idempotent_same_observed_at_is_no_op(self, session_factory) -> None:
        """T3: applying same observed_at twice leaves DB unchanged; second summary has rows_skipped=1."""
        async with session_factory() as session:
            subject_id = await _make_employee_subject(session)
            app_id, resource_id = await _make_app_and_resource(session)
            fact_id = await _make_access_fact(session, subject_id, resource_id)
            await _make_initiative(session, fact_id)
            await session.commit()

        async with session_factory() as session:
            log1 = CapturingLogService()
            svc1 = EffectiveAccessProjectionService(session, log1)  # type: ignore[arg-type]
            await svc1.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T0,
            )
            await session.commit()

        async with session_factory() as session:
            log2 = CapturingLogService()
            svc2 = EffectiveAccessProjectionService(session, log2)  # type: ignore[arg-type]
            summary2 = await svc2.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T0,
            )

            assert summary2.rows_inserted == 0
            assert summary2.rows_updated == 0
            assert summary2.rows_skipped == 1

            # DB row must still have original observed_at
            row = await session.execute(
                sa.text('SELECT observed_at FROM effective_grants WHERE source_access_fact_id = :fid'),
                {'fid': fact_id},
            )
            assert row.scalar_one() == _T0

            # Two events emitted across two calls (idempotency is DB-level, not emission-level)
            assert len(log2.events) == 1
            assert log2.events[0][0] == 'eas.projection.completed'

            await session.rollback()

    @pytest.mark.asyncio
    async def test_apply_rejects_stale_observed_at(self, session_factory) -> None:
        """T4: applying at T0 < T1 after an apply at T1 keeps DB row at T1 (rows_skipped=1)."""
        async with session_factory() as session:
            subject_id = await _make_employee_subject(session)
            app_id, resource_id = await _make_app_and_resource(session)
            fact_id = await _make_access_fact(session, subject_id, resource_id)
            await _make_initiative(session, fact_id)
            await session.commit()

        async with session_factory() as session:
            log1 = CapturingLogService()
            svc1 = EffectiveAccessProjectionService(session, log1)  # type: ignore[arg-type]
            await svc1.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T1,
            )
            await session.commit()

        async with session_factory() as session:
            log2 = CapturingLogService()
            svc2 = EffectiveAccessProjectionService(session, log2)  # type: ignore[arg-type]
            summary = await svc2.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T0,
            )

            assert summary.rows_skipped == 1
            assert summary.rows_updated == 0

            row = await session.execute(
                sa.text('SELECT observed_at FROM effective_grants WHERE source_access_fact_id = :fid'),
                {'fid': fact_id},
            )
            assert row.scalar_one() == _T1

            await session.rollback()

    @pytest.mark.asyncio
    async def test_apply_invalidate_tombstones_rows(self, session_factory) -> None:
        """T5: invalidate at T1 > T0 tombstones live rows; emits incremental/invalidate event."""
        async with session_factory() as session:
            subject_id = await _make_employee_subject(session)
            app_id, resource_id = await _make_app_and_resource(session)
            fact_id = await _make_access_fact(session, subject_id, resource_id)
            await _make_initiative(session, fact_id)
            await session.commit()

        async with session_factory() as session:
            log1 = CapturingLogService()
            svc1 = EffectiveAccessProjectionService(session, log1)  # type: ignore[arg-type]
            await svc1.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T0,
            )
            await session.commit()

        async with session_factory() as session:
            log2 = CapturingLogService()
            svc2 = EffectiveAccessProjectionService(session, log2)  # type: ignore[arg-type]
            summary = await svc2.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.INVALIDATE_FACT,
                observed_at=_T1,
            )

            assert summary.rows_tombstoned == 1
            assert summary.rows_inserted == 0
            assert summary.rows_updated == 0
            assert summary.rows_skipped == 0
            assert summary.pairs_projected == 0

            row = await session.execute(
                sa.text('SELECT tombstoned_at, observed_at FROM effective_grants WHERE source_access_fact_id = :fid'),
                {'fid': fact_id},
            )
            grant = row.one()
            assert grant.tombstoned_at == _T1
            assert grant.observed_at == _T1

            assert len(log2.events) == 1
            event_type, _, _, _, payload, _ = log2.events[0]
            assert event_type == 'eas.projection.completed'
            assert payload['mode'] == 'incremental'
            assert payload['change_kind'] == 'invalidate_fact'
            assert payload['rows_tombstoned'] == 1

            await session.rollback()

    # -----------------------------------------------------------------------
    # S-I1 — INVALIDATE_INITIATIVE tombstones only matching grants (Step 6a)
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_apply_invalidate_initiative_tombstones_only_matching_grants(self, session_factory) -> None:
        """S-I1: INVALIDATE_INITIATIVE tombstones only the expired initiative's grants."""
        async with session_factory() as session:
            subject_id = await _make_employee_subject(session)
            app_id, resource_id = await _make_app_and_resource(session)
            fact_id = await _make_access_fact(session, subject_id, resource_id)
            init_a = await _make_initiative(session, fact_id)
            init_b = await _make_initiative(session, fact_id)
            await session.commit()

        # Seed both grants via UPSERT
        async with session_factory() as session:
            log1 = CapturingLogService()
            svc1 = EffectiveAccessProjectionService(session, log1)  # type: ignore[arg-type]
            await svc1.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T0,
            )
            await session.commit()

        causation_id = uuid.UUID('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')

        async with session_factory() as session:
            log2 = CapturingLogService()
            svc2 = EffectiveAccessProjectionService(session, log2)  # type: ignore[arg-type]
            summary = await svc2.apply_incremental_change(
                change_kind=IncrementalApplyKind.INVALIDATE_INITIATIVE,
                initiative_id=init_a,
                observed_at=_T1,
                causation_event_id=causation_id,
            )

            # Only one row tombstoned (init_a), init_b untouched
            assert summary.rows_tombstoned == 1
            assert summary.rows_inserted == 0
            assert summary.rows_updated == 0
            assert summary.rows_skipped == 0
            assert summary.pairs_projected == 0
            assert summary.scope_kind == ProjectionScopeKind.INITIATIVE
            assert summary.scope_id == init_a

            # Verify DB state
            row_a = await session.execute(
                sa.text('SELECT tombstoned_at, observed_at FROM effective_grants WHERE source_initiative_id = :iid'),
                {'iid': init_a},
            )
            grant_a = row_a.one()
            assert grant_a.tombstoned_at == _T1
            assert grant_a.observed_at == _T1

            row_b = await session.execute(
                sa.text('SELECT tombstoned_at FROM effective_grants WHERE source_initiative_id = :iid'),
                {'iid': init_b},
            )
            grant_b = row_b.one()
            assert grant_b.tombstoned_at is None

            # Event assertions
            assert len(log2.events) == 1
            event_type, level, _, _, payload, _ = log2.events[0]
            assert event_type == 'eas.projection.completed'
            assert level == LogLevel.INFO
            assert payload['mode'] == 'incremental'
            assert payload['change_kind'] == 'invalidate_initiative'
            assert payload['scope_kind'] == 'initiative'
            assert payload['scope_id'] == str(init_a)
            assert payload['rows_tombstoned'] == 1
            assert payload['pairs_projected'] == 0
            assert payload['rows_upserted'] == 0
            assert payload['causation_event_id'] == str(causation_id)

            await session.rollback()

    # -----------------------------------------------------------------------
    # S-D1 — UPSERT tombstones disappeared pairs (Step 6b)
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_upsert_tombstones_disappeared_pairs(self, session_factory) -> None:
        """S-D1: UPSERT reprojection tombstones the grant whose initiative has
        disappeared from the fact's live set (silent-shrink guard).

        Seam: init_b belongs to a different fact (fact_b), so
        _fetch_initiatives_for_facts(fact_id) never returns it.  We seed a
        grant for (fact_id, init_b) directly via upsert_effective_grants to
        simulate a "hanging" row left from a previous projection run — this is
        the set-difference scenario Step 6b covers without relying on CASCADE
        behaviour of the FK.
        """
        async with session_factory() as session:
            subject_id = await _make_employee_subject(session)
            app_id, resource_id = await _make_app_and_resource(session)
            _, resource_b_id = await _make_app_and_resource(session)
            # fact_id is the fact under test; fact_b is only needed so init_b has
            # a valid access_fact_id FK reference.
            fact_id = await _make_access_fact(session, subject_id, resource_id)
            fact_b = await _make_access_fact(session, subject_id, resource_b_id)
            init_a = await _make_initiative(session, fact_id)
            init_b = await _make_initiative(session, fact_b)  # belongs to OTHER fact
            await session.commit()

        # Seed both grants directly — simulates the state after a previous
        # projection that included init_b (which was then "moved away").
        async with session_factory() as session:
            await upsert_effective_grants(
                session,
                [
                    EffectiveGrantDraft(
                        subject_id=subject_id,
                        subject_kind=SubjectKind.employee,
                        application_id=app_id,
                        account_id=None,
                        resource_id=resource_id,
                        action=Action.read,
                        effect=EffectiveGrantEffect.allow,
                        initiative_type=InitiativeType.birthright,
                        initiative_origin='test-origin',
                        valid_from=_NOW,
                        valid_until=None,
                        source_access_fact_id=fact_id,
                        source_initiative_id=init_a,
                        observed_at=_T0,
                        tombstoned_at=None,
                    ),
                    EffectiveGrantDraft(
                        subject_id=subject_id,
                        subject_kind=SubjectKind.employee,
                        application_id=app_id,
                        account_id=None,
                        resource_id=resource_id,
                        action=Action.read,
                        effect=EffectiveGrantEffect.allow,
                        initiative_type=InitiativeType.birthright,
                        initiative_origin='test-origin',
                        valid_from=_NOW,
                        valid_until=None,
                        source_access_fact_id=fact_id,
                        source_initiative_id=init_b,
                        observed_at=_T0,
                        tombstoned_at=None,
                    ),
                ],
            )
            await session.commit()

        causation_id = uuid.UUID('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb')

        # UPSERT at T1: only init_a is in the live set for fact_id; the grant
        # for init_b must be tombstoned by set-diff.
        async with session_factory() as session:
            log2 = CapturingLogService()
            svc2 = EffectiveAccessProjectionService(session, log2)  # type: ignore[arg-type]
            summary = await svc2.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T1,
                causation_event_id=causation_id,
            )

            # Diff counts. The projector emits a draft for init_a only;
            # init_b is not in the live set — its grant is tombstoned by set-diff.
            assert summary.rows_tombstoned == 1
            assert summary.rows_inserted == 0
            assert summary.rows_updated == 1
            assert summary.rows_skipped == 0
            assert summary.pairs_projected == 1

            # init_a grant: observed_at bumped to T1, not tombstoned
            row_a = await session.execute(
                sa.text(
                    'SELECT tombstoned_at, observed_at FROM effective_grants '
                    'WHERE source_initiative_id = :iid AND source_access_fact_id = :fid'
                ),
                {'iid': init_a, 'fid': fact_id},
            )
            g_a = row_a.one()
            assert g_a.tombstoned_at is None
            assert g_a.observed_at == _T1

            # init_b grant: tombstoned at T1 by set-diff
            row_b = await session.execute(
                sa.text(
                    'SELECT tombstoned_at, observed_at FROM effective_grants '
                    'WHERE source_initiative_id = :iid AND source_access_fact_id = :fid'
                ),
                {'iid': init_b, 'fid': fact_id},
            )
            g_b = row_b.one()
            assert g_b.tombstoned_at == _T1
            assert g_b.observed_at == _T1

            # Event payload
            assert len(log2.events) == 1
            event_type, level, _, _, payload, _ = log2.events[0]
            assert event_type == 'eas.projection.completed'
            assert level == LogLevel.INFO
            assert payload['mode'] == 'incremental'
            assert payload['change_kind'] == 'upsert'
            assert payload['scope_kind'] == 'access_fact'
            assert payload['scope_id'] == str(fact_id)
            assert payload['rows_tombstoned'] == 1
            assert payload['pairs_projected'] == 1
            assert payload['rows_updated'] == 1
            assert payload['rows_inserted'] == 0
            assert payload['causation_event_id'] == str(causation_id)

            await session.rollback()

    # -----------------------------------------------------------------------
    # S-D2 — UPSERT without initiative drop emits rows_tombstoned=0 (Step 6b)
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_upsert_no_drop_no_extra_tombstones(self, session_factory) -> None:
        """S-D2: UPSERT without any initiative disappearance emits
        rows_tombstoned=0 — guards against an over-broad set-diff predicate."""
        async with session_factory() as session:
            subject_id = await _make_employee_subject(session)
            app_id, resource_id = await _make_app_and_resource(session)
            fact_id = await _make_access_fact(session, subject_id, resource_id)
            await _make_initiative(session, fact_id)
            await _make_initiative(session, fact_id)
            await session.commit()

        # First UPSERT at T0
        async with session_factory() as session:
            log1 = CapturingLogService()
            svc1 = EffectiveAccessProjectionService(session, log1)  # type: ignore[arg-type]
            await svc1.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T0,
            )
            await session.commit()

        # Second UPSERT at T1 — no drops
        async with session_factory() as session:
            log2 = CapturingLogService()
            svc2 = EffectiveAccessProjectionService(session, log2)  # type: ignore[arg-type]
            summary = await svc2.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T1,
            )

            assert summary.rows_tombstoned == 0
            assert summary.rows_updated == 2
            assert summary.rows_inserted == 0
            assert summary.rows_skipped == 0
            assert summary.pairs_projected == 2

            # Both grants still live
            rows = await session.execute(
                sa.text(
                    'SELECT tombstoned_at, observed_at FROM effective_grants '
                    'WHERE source_access_fact_id = :fid ORDER BY source_initiative_id'
                ),
                {'fid': fact_id},
            )
            for row in rows.all():
                assert row.tombstoned_at is None
                assert row.observed_at == _T1

            payload = log2.events[0][4]
            assert payload['rows_tombstoned'] == 0

            await session.rollback()

    # -----------------------------------------------------------------------
    # S-D3 — all initiatives disappeared: every grant tombstoned (Step 6b)
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_upsert_all_initiatives_disappeared_tombstones_all(self, session_factory) -> None:
        """S-D3: UPSERT reprojection on a fact whose live set is empty tombstones
        every grant for the fact. Guards the empty-drafts corner of the UPSERT
        branch (Edit B else arm); anti-regression against wrapping the set-diff
        helper in an ``if drafts:`` guard.

        Seam: fact_id has no initiatives of its own; both init_a and init_b
        belong to fact_b.  Two grants are seeded directly via upsert_effective_grants
        with source_access_fact_id=fact_id — simulating a "childless" projection
        run where all previously-contributing initiatives have disappeared.
        """
        async with session_factory() as session:
            subject_id = await _make_employee_subject(session)
            app_id, resource_id = await _make_app_and_resource(session)
            _, resource_b_id = await _make_app_and_resource(session)
            # fact_id: the fact under test — it has no initiatives of its own.
            # fact_b: a sibling fact that "owns" init_a and init_b.
            fact_id = await _make_access_fact(session, subject_id, resource_id)
            fact_b = await _make_access_fact(session, subject_id, resource_b_id)
            init_a = await _make_initiative(session, fact_b)  # belongs to fact_b
            init_b = await _make_initiative(session, fact_b)  # belongs to fact_b
            await session.commit()

        # Seed two grants for fact_id referencing init_a and init_b — simulates
        # a previous projection that included these initiatives.
        async with session_factory() as session:
            await upsert_effective_grants(
                session,
                [
                    EffectiveGrantDraft(
                        subject_id=subject_id,
                        subject_kind=SubjectKind.employee,
                        application_id=app_id,
                        account_id=None,
                        resource_id=resource_id,
                        action=Action.read,
                        effect=EffectiveGrantEffect.allow,
                        initiative_type=InitiativeType.birthright,
                        initiative_origin='test-origin',
                        valid_from=_NOW,
                        valid_until=None,
                        source_access_fact_id=fact_id,
                        source_initiative_id=init_a,
                        observed_at=_T0,
                        tombstoned_at=None,
                    ),
                    EffectiveGrantDraft(
                        subject_id=subject_id,
                        subject_kind=SubjectKind.employee,
                        application_id=app_id,
                        account_id=None,
                        resource_id=resource_id,
                        action=Action.read,
                        effect=EffectiveGrantEffect.allow,
                        initiative_type=InitiativeType.birthright,
                        initiative_origin='test-origin',
                        valid_from=_NOW,
                        valid_until=None,
                        source_access_fact_id=fact_id,
                        source_initiative_id=init_b,
                        observed_at=_T0,
                        tombstoned_at=None,
                    ),
                ],
            )
            await session.commit()

        # UPSERT at T1 — fact_id has no initiatives → live set is empty.
        # Helper must tombstone both grants; service must NOT fall through to
        # INVALIDATE_FACT; event payload must read change_kind='upsert'.
        async with session_factory() as session:
            log2 = CapturingLogService()
            svc2 = EffectiveAccessProjectionService(session, log2)  # type: ignore[arg-type]
            summary = await svc2.apply_incremental_change(
                access_fact_id=fact_id,
                change_kind=IncrementalApplyKind.UPSERT,
                observed_at=_T1,
            )

            assert summary.rows_tombstoned == 2
            assert summary.rows_inserted == 0
            assert summary.rows_updated == 0
            assert summary.rows_skipped == 0
            assert summary.pairs_projected == 0

            # Both grants tombstoned at T1
            rows = await session.execute(
                sa.text('SELECT tombstoned_at, observed_at FROM effective_grants WHERE source_access_fact_id = :fid'),
                {'fid': fact_id},
            )
            for row in rows.all():
                assert row.tombstoned_at == _T1
                assert row.observed_at == _T1

            # Event payload — change_kind stays 'upsert' (fact is live, children gone)
            assert len(log2.events) == 1
            payload = log2.events[0][4]
            assert payload['change_kind'] == 'upsert'
            assert payload['scope_kind'] == 'access_fact'
            assert payload['rows_tombstoned'] == 2
            assert payload['pairs_projected'] == 0

            await session.rollback()

    # -----------------------------------------------------------------------
    # T-precondition-xor — strict XOR guard on access_fact_id / initiative_id
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        'kwargs,expected_fragment',
        [
            # UPSERT with initiative_id but no access_fact_id → co-presence error first
            (
                {'change_kind': IncrementalApplyKind.UPSERT, 'initiative_id': uuid.uuid4()},
                'must not receive initiative_id',
            ),
            # INVALIDATE_FACT with initiative_id but no access_fact_id → co-presence error first
            (
                {'change_kind': IncrementalApplyKind.INVALIDATE_FACT, 'initiative_id': uuid.uuid4()},
                'must not receive initiative_id',
            ),
            # INVALIDATE_INITIATIVE without initiative_id
            (
                {'change_kind': IncrementalApplyKind.INVALIDATE_INITIATIVE, 'access_fact_id': uuid.uuid4()},
                'must not receive access_fact_id',
            ),
            # UPSERT with both ids
            (
                {
                    'change_kind': IncrementalApplyKind.UPSERT,
                    'access_fact_id': uuid.uuid4(),
                    'initiative_id': uuid.uuid4(),
                },
                'must not receive initiative_id',
            ),
            # INVALIDATE_FACT with both ids
            (
                {
                    'change_kind': IncrementalApplyKind.INVALIDATE_FACT,
                    'access_fact_id': uuid.uuid4(),
                    'initiative_id': uuid.uuid4(),
                },
                'must not receive initiative_id',
            ),
            # INVALIDATE_INITIATIVE with no id at all
            (
                {'change_kind': IncrementalApplyKind.INVALIDATE_INITIATIVE},
                'requires initiative_id',
            ),
            # UPSERT with no id at all → missing required id
            (
                {'change_kind': IncrementalApplyKind.UPSERT},
                'requires access_fact_id',
            ),
            # INVALIDATE_FACT with no id at all → missing required id
            (
                {'change_kind': IncrementalApplyKind.INVALIDATE_FACT},
                'requires access_fact_id',
            ),
        ],
    )
    async def test_apply_incremental_change_rejects_invalid_id_combos(
        self, session_factory, kwargs: dict, expected_fragment: str
    ) -> None:
        """T-precondition-xor: service raises ValueError before any DB work on bad id combos."""
        async with session_factory() as session:
            log = CapturingLogService()
            svc = EffectiveAccessProjectionService(session, log)  # type: ignore[arg-type]
            with pytest.raises(ValueError, match=expected_fragment):
                await svc.apply_incremental_change(observed_at=_T0, **kwargs)
            # No DB side-effects, no events
            assert log.events == []
            await session.rollback()
