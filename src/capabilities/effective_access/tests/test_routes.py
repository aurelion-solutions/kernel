# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for EAS read endpoints — 9 cases (A1–A9).

Uses the shared ``client`` fixture from the root conftest which wires the full
``v0.py`` router (including ``effective_grants_router``) against the test DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.capabilities.effective_access.models import EffectiveGrantEffect
from src.capabilities.effective_access.projector import EffectiveGrantDraft
from src.capabilities.effective_access.repository import upsert_effective_grants
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_BASE = '/api/v0/effective-grants'


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _make_employee_subject(session: AsyncSession) -> UUID:
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


async def _make_app_and_resource(session: AsyncSession) -> tuple[UUID, UUID]:
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


async def _make_access_fact(session: AsyncSession, subject_id: UUID, resource_id: UUID) -> UUID:
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


async def _make_initiative(session: AsyncSession, access_fact_id: UUID) -> UUID:
    from src.inventory.initiatives.models import Initiative

    init = Initiative(
        access_fact_id=access_fact_id,
        type=InitiativeType.birthright,
        origin='test-origin',
        valid_from=_NOW,
    )
    session.add(init)
    await session.flush()
    return init.id


def _draft(
    subject_id: UUID,
    subject_kind: SubjectKind,
    app_id: UUID,
    resource_id: UUID,
    fact_id: UUID,
    init_id: UUID,
    *,
    effect: EffectiveGrantEffect = EffectiveGrantEffect.allow,
) -> EffectiveGrantDraft:
    return EffectiveGrantDraft(
        subject_id=subject_id,
        subject_kind=subject_kind,
        application_id=app_id,
        account_id=None,
        resource_id=resource_id,
        action=Action.read,
        effect=effect,
        initiative_type=InitiativeType.birthright,
        initiative_origin='test-origin',
        valid_from=_NOW,
        valid_until=None,
        source_access_fact_id=fact_id,
        source_initiative_id=init_id,
        observed_at=_NOW,
        tombstoned_at=None,
    )


async def _seed_one_grant(engine, subject_id: UUID | None = None) -> dict:
    """Seed one grant row; return dict with ids for assertions."""
    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        sub_id = subject_id or await _make_employee_subject(session)
        app_id, res_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, sub_id, res_id)
        init_id = await _make_initiative(session, fact_id)
        d = _draft(sub_id, SubjectKind.employee, app_id, res_id, fact_id, init_id)
        await upsert_effective_grants(session, [d])
        await session.flush()
        from sqlalchemy import text

        row = await session.execute(
            text('SELECT id FROM effective_grants WHERE source_initiative_id = :iid'),
            {'iid': init_id},
        )
        grant_id = row.scalar_one()
        await session.commit()
    return {
        'grant_id': grant_id,
        'subject_id': sub_id,
        'application_id': app_id,
        'resource_id': res_id,
        'init_id': init_id,
    }


# ---------------------------------------------------------------------------
# A1 — no filters → HTTP 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_no_filters_returns_400(client) -> None:
    """GET /effective-grants with no mandatory filter returns 400."""
    response = await client.get(_BASE)
    assert response.status_code == 400
    assert 'at least one of' in response.json()['detail']


# ---------------------------------------------------------------------------
# A2 — subject_id filter → 200, items conform to EffectiveGrantRead shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_by_subject_id_returns_200(client, engine) -> None:
    ids = await _seed_one_grant(engine)
    response = await client.get(_BASE, params={'subject_id': str(ids['subject_id']), 'active_only': 'false'})
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    item = data[0]
    assert 'id' in item
    assert 'subject_id' in item
    assert 'effect' in item
    assert 'observed_at' in item


# ---------------------------------------------------------------------------
# A3 — source_initiative_id filter → 200, all rows for that initiative
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_by_source_initiative_id(client, engine) -> None:
    ids = await _seed_one_grant(engine)
    response = await client.get(_BASE, params={'source_initiative_id': str(ids['init_id']), 'active_only': 'false'})
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]['source_initiative_id'] == str(ids['init_id'])


# ---------------------------------------------------------------------------
# A4 — limit > 1000 → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_limit_over_max_returns_422(client) -> None:
    response = await client.get(_BASE, params={'subject_id': str(uuid.uuid4()), 'limit': 2000})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# A5 — pagination: seed 5 rows, limit=2 pages are disjoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pagination_disjoint_pages(client, engine) -> None:
    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        subject_id = await _make_employee_subject(session)
        app_id, res_id = await _make_app_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, res_id)
        for _ in range(5):
            init_id = await _make_initiative(session, fact_id)
            d = _draft(subject_id, SubjectKind.employee, app_id, res_id, fact_id, init_id)
            await upsert_effective_grants(session, [d])
        await session.commit()

    page1 = await client.get(
        _BASE, params={'subject_id': str(subject_id), 'limit': 2, 'offset': 0, 'active_only': 'false'}
    )
    page2 = await client.get(
        _BASE, params={'subject_id': str(subject_id), 'limit': 2, 'offset': 2, 'active_only': 'false'}
    )
    assert page1.status_code == 200
    assert page2.status_code == 200

    ids1 = {item['id'] for item in page1.json()}
    ids2 = {item['id'] for item in page2.json()}
    assert ids1.isdisjoint(ids2)


# ---------------------------------------------------------------------------
# A6 — by id: 200 on hit, 404 on miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_by_id_hit_and_miss(client, engine) -> None:
    ids = await _seed_one_grant(engine)
    response_hit = await client.get(f'{_BASE}/{ids["grant_id"]}')
    assert response_hit.status_code == 200
    assert response_hit.json()['id'] == str(ids['grant_id'])

    response_miss = await client.get(f'{_BASE}/{uuid.uuid4()}')
    assert response_miss.status_code == 404


# ---------------------------------------------------------------------------
# A7 — explain: 200 with correct shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_returns_200_with_shape(client, engine) -> None:
    ids = await _seed_one_grant(engine)
    response = await client.get(
        f'{_BASE}/explain',
        params={
            'subject_id': str(ids['subject_id']),
            'resource_id': str(ids['resource_id']),
            'action': 'read',
            'active_only': 'false',
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert 'effect' in data
    assert data['effect'] in ('allow', 'deny', 'none')
    assert 'grants' in data
    assert isinstance(data['grants'], list)


# ---------------------------------------------------------------------------
# A8 — explain: missing required param → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_missing_param_returns_422(client) -> None:
    # Missing action
    response = await client.get(
        f'{_BASE}/explain',
        params={'subject_id': str(uuid.uuid4()), 'resource_id': str(uuid.uuid4())},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# A9 — route-ordering pin: /explain must not be dispatched to /{grant_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_ordering_explain_not_uuid(client) -> None:
    """GET /effective-grants/explain?... must return 200 or 422, NOT a UUID parse 422.

    If ``/explain`` is declared after ``/{grant_id}``, FastAPI tries to parse the
    literal string ``explain`` as a UUID and returns 422 with a UUID-parse error.
    This test distinguishes that case from a valid 422 (missing required params):
    it supplies all required params, so the only way to get a non-200 response is
    if the route ordering is wrong.

    We also test with an unknown subject_id (no rows → effect='none') so the
    endpoint returns 200.
    """
    response = await client.get(
        f'{_BASE}/explain',
        params={
            'subject_id': str(uuid.uuid4()),
            'resource_id': str(uuid.uuid4()),
            'action': 'read',
        },
    )
    # Must be 200 (zero rows → effect='none'), NOT 422 from UUID parse failure
    assert response.status_code == 200, (
        f'Expected 200 from /explain but got {response.status_code}. '
        'This typically means /explain was declared after /{grant_id} in routes.py '
        'and FastAPI tried to parse "explain" as a UUID.'
    )
    assert response.json()['effect'] == 'none'
