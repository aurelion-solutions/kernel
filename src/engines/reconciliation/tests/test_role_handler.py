# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for the role artifact handler.

Uses real PG session (session_factory fixture) so Account lookups work.
ResourceService is mocked to avoid creating real resources.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest
from src.engines.reconciliation.handlers.role import RoleHandler
from src.inventory.access_artifacts.schemas import AccessArtifactView
from src.inventory.accounts.models import Account
from src.inventory.subjects.models import Subject, SubjectKind  # noqa: F401
from src.platform.applications.models import Application


async def _make_application(session) -> uuid.UUID:
    app = Application(
        name=f'role-handler-test-{uuid.uuid4().hex[:8]}',
        code=f'rh-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


async def _make_subject(session) -> uuid.UUID:
    from src.inventory.employees.repository import create_employee  # noqa: PLC0415
    from src.inventory.persons.repository import create_person  # noqa: PLC0415

    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='Test User')
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


_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _make_artifact(payload: dict, application_id: uuid.UUID | None = None) -> AccessArtifactView:
    app_id = application_id or uuid.uuid4()
    return AccessArtifactView(
        id=uuid.uuid4(),
        artifact_type='role',
        application_id=app_id,
        external_id='test:ext',
        payload=payload,
        raw_name=None,
        effect=None,
        is_active=True,
        tombstoned_at=None,
        observed_at=_NOW,
        ingested_at=_NOW,
        ingest_batch_id=None,
        valid_from=None,
        valid_until=None,
    )


def _make_resource_service(resource_id: uuid.UUID | None = None) -> MagicMock:
    svc = MagicMock()
    fake_resource = MagicMock()
    fake_resource.id = resource_id or uuid.uuid4()
    svc.ensure_resource_by_identity = AsyncMock(return_value=fake_resource)
    return svc


@pytest.mark.asyncio
async def test_happy_path(session_factory):
    """Valid payload + account in DB → single NormalizationResult."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        subject_id = await _make_subject(session)
        account = Account(
            application_id=app_id,
            username='alice',
            subject_id=subject_id,
        )
        session.add(account)
        await session.flush()
        account_id = account.id

        resource_id = uuid.uuid4()
        svc = _make_resource_service(resource_id)
        handler = RoleHandler(resource_service=svc)

        artifact = _make_artifact(
            payload={
                'account_external_id': 'alice',
                'resource_key': 'reports',
                'resource_type': 'report',
                'action_slug': 'view',
                'effect': 'allow',
            },
            application_id=app_id,
        )

        results = await handler.handle(artifact, session)

    assert len(results) == 1
    r = results[0]
    assert r.subject_id == subject_id
    assert r.account_id == account_id
    assert r.resource_id == resource_id
    assert r.action_slug == 'view'
    assert r.effect == 'allow'


@pytest.mark.asyncio
async def test_account_not_found_returns_empty(session_factory):
    """account_external_id not in DB → []."""
    app_id = uuid.uuid4()
    handler = RoleHandler(resource_service=_make_resource_service())

    async with session_factory() as session:
        artifact = _make_artifact(
            payload={
                'account_external_id': 'nobody',
                'resource_key': 'r',
                'resource_type': 't',
                'action_slug': 'read',
                'effect': 'allow',
            },
            application_id=app_id,
        )
        results = await handler.handle(artifact, session)

    assert results == []


@pytest.mark.asyncio
async def test_account_without_subject_returns_result(session_factory):
    """Account exists but subject_id is None → result with account_id, subject_id=None."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        account = Account(
            application_id=app_id,
            username='orphan',
            subject_id=None,
        )
        session.add(account)
        await session.flush()
        account_id = account.id

        resource_id = uuid.uuid4()
        handler = RoleHandler(resource_service=_make_resource_service(resource_id))
        artifact = _make_artifact(
            payload={
                'account_external_id': 'orphan',
                'resource_key': 'r',
                'resource_type': 't',
                'action_slug': 'read',
                'effect': 'allow',
            },
            application_id=app_id,
        )
        results = await handler.handle(artifact, session)

    assert len(results) == 1
    assert results[0].account_id == account_id
    assert results[0].subject_id is None


@pytest.mark.asyncio
async def test_invalid_payload_returns_empty(session_factory):
    """Missing required fields → []."""
    handler = RoleHandler(resource_service=_make_resource_service())

    async with session_factory() as session:
        artifact = _make_artifact(payload={'junk': 'data'})
        results = await handler.handle(artifact, session)

    assert results == []


@pytest.mark.asyncio
async def test_old_subject_id_payload_returns_empty(session_factory):
    """Payload with subject_id UUID (old format) instead of account_external_id → []."""
    handler = RoleHandler(resource_service=_make_resource_service())

    async with session_factory() as session:
        artifact = _make_artifact(
            payload={
                'subject_id': str(uuid.uuid4()),  # old format — should not be accepted
                'resource_key': 'r',
                'resource_type': 't',
                'action_slug': 'read',
                'effect': 'allow',
            }
        )
        results = await handler.handle(artifact, session)

    assert results == []
