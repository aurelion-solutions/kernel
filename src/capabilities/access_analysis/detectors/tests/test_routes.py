# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP tests for POST /access-analysis/detect-orphans."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.findings.models import Finding
from src.inventory.accounts.models import Account
from src.inventory.nhi.models import NHI
from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind
from src.platform.applications.models import Application

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_application(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    app = Application(
        name=f'app-{uuid.uuid4().hex[:8]}',
        code=f'code-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


async def _seed_subject(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    nhi = NHI(
        external_id=f'nhi-{uuid.uuid4().hex[:8]}',
        name=f'test-nhi-{uuid.uuid4().hex[:8]}',
        kind='service_account',
        owner_employee_id=None,
    )
    session.add(nhi)
    await session.flush()
    subject = Subject(
        external_id=f'subj-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status='active',
    )
    session.add(subject)
    await session.flush()
    return subject.id


async def _seed_orphan_account(session, app_id: uuid.UUID, username: str = 'orphan') -> uuid.UUID:  # type: ignore[no-untyped-def]
    account = Account(
        application_id=app_id,
        username=username,
        subject_id=None,
    )
    session.add(account)
    await session.flush()
    return account.id


async def _seed_owned_account(session, app_id: uuid.UUID, subject_id: uuid.UUID) -> uuid.UUID:  # type: ignore[no-untyped-def]
    account = Account(
        application_id=app_id,
        username='owned',
        subject_id=subject_id,
    )
    session.add(account)
    await session.flush()
    return account.id


# ---------------------------------------------------------------------------
# Test A1: empty body → 200, empty list when no orphans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_orphans_empty_body_no_orphans(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post('/api/v0/access-analysis/detect-orphans', json={})
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Test A2: POST with seeded orphans → 200, sorted list, UUIDs as strings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_orphans_returns_sorted_findings(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        await _seed_orphan_account(session, app_id, 'zeta')
        await _seed_orphan_account(session, app_id, 'alpha')
        await session.commit()

    resp = await client.post('/api/v0/access-analysis/detect-orphans', json={})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2

    # UUIDs are strings
    assert isinstance(data[0]['account_id'], str)
    assert isinstance(data[0]['application_id'], str)

    # Sorted by (application_id, username, account_id) — alpha < zeta
    usernames = [d['username'] for d in data]
    assert usernames == sorted(usernames)


# ---------------------------------------------------------------------------
# Test A3: POST with application_id filter → only matching orphans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_orphans_application_id_filter(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_a = await _seed_application(session)
        app_b = await _seed_application(session)
        await _seed_orphan_account(session, app_a, 'orphan_a')
        await _seed_orphan_account(session, app_b, 'orphan_b')
        await session.commit()

    resp = await client.post(
        '/api/v0/access-analysis/detect-orphans',
        json={'application_id': str(app_a)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]['username'] == 'orphan_a'
    assert data[0]['application_id'] == str(app_a)


# ---------------------------------------------------------------------------
# Test A4: limit exceeding hard cap → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_orphans_limit_exceeds_cap_422(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post('/api/v0/access-analysis/detect-orphans', json={'limit': 9999})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test A5: malformed body (extra field) → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_orphans_extra_field_422(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post(
        '/api/v0/access-analysis/detect-orphans',
        json={'extra_field': 'bad'},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test A6: no Finding rows written after detection call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_orphans_writes_no_findings(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        await _seed_orphan_account(session, app_id, 'ghost')
        await session.commit()

    resp = await client.post('/api/v0/access-analysis/detect-orphans', json={})
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    async with session_factory() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(Finding))
    assert count == 0
