# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for reconciliation.handlers.db_grant."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.engines.inventory_reconcile.handlers.db_grant import DbGrantHandler
from src.engines.inventory_reconcile.registry import _reset_registry_for_tests, get_handler, register_handler


async def _make_application(session) -> uuid.UUID:
    from src.platform.applications.models import Application

    app = Application(
        name=f'dbgrant-handler-test-{uuid.uuid4()}',
        code=f'dg-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


def _make_artifact(application_id: uuid.UUID, payload: dict, artifact_type: str = 'db_grant'):
    from src.inventory.access_artifacts.schemas import AccessArtifactView

    now = datetime.now(UTC)
    return AccessArtifactView(
        id=uuid.uuid4(),
        application_id=application_id,
        artifact_type=artifact_type,
        external_id=str(uuid.uuid4()),
        payload=payload,
        raw_name=None,
        effect=None,
        valid_from=None,
        valid_until=None,
        is_active=True,
        tombstoned_at=None,
        observed_at=now,
        ingested_at=now,
        ingest_batch_id=None,
    )


def _base_payload(subject_id: uuid.UUID, privileges: list[str]) -> dict:
    return {
        'subject_id': str(subject_id),
        'resource_type': 'db_table',
        'resource_key': 'finance.invoices',
        'privileges': privileges,
        'effect': 'allow',
    }


def test_db_grant_handler_registered_at_import():
    """DbGrantHandler can be registered and retrieved from the registry."""
    _reset_registry_for_tests()
    assert get_handler('db_grant') is None

    register_handler('db_grant', DbGrantHandler())
    assert get_handler('db_grant') is not None
    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_db_grant_handler_single_privilege(session_factory):
    """privileges=['SELECT'] → one result with action_slug='read'."""
    _reset_registry_for_tests()
    register_handler('db_grant', DbGrantHandler())
    handler = get_handler('db_grant')
    assert handler is not None

    subject_id = uuid.uuid4()
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact = _make_artifact(app_id, _base_payload(subject_id, ['SELECT']))
        results = await handler.handle(artifact, session)

    assert len(results) == 1
    assert results[0].action_slug == 'read'
    assert results[0].subject_id == subject_id

    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_db_grant_handler_multiple_distinct_privileges(session_factory):
    """privileges=['SELECT', 'EXECUTE'] → two results (read, execute)."""
    _reset_registry_for_tests()
    register_handler('db_grant', DbGrantHandler())
    handler = get_handler('db_grant')
    assert handler is not None

    subject_id = uuid.uuid4()
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact = _make_artifact(app_id, _base_payload(subject_id, ['SELECT', 'EXECUTE']))
        results = await handler.handle(artifact, session)

    assert len(results) == 2
    slugs = {r.action_slug for r in results}
    assert slugs == {'read', 'execute'}

    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_db_grant_handler_dedupe_same_slug(session_factory):
    """INSERT + UPDATE + DELETE all map to 'write' → only one result after dedup."""
    _reset_registry_for_tests()
    register_handler('db_grant', DbGrantHandler())
    handler = get_handler('db_grant')
    assert handler is not None

    subject_id = uuid.uuid4()
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact = _make_artifact(app_id, _base_payload(subject_id, ['INSERT', 'UPDATE', 'DELETE']))
        results = await handler.handle(artifact, session)

    assert len(results) == 1
    assert results[0].action_slug == 'write'

    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_db_grant_handler_unknown_privilege_silently_dropped(session_factory):
    """privileges=['TRUNCATE'] → [] (no exception, no fact)."""
    _reset_registry_for_tests()
    register_handler('db_grant', DbGrantHandler())
    handler = get_handler('db_grant')
    assert handler is not None

    subject_id = uuid.uuid4()
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact = _make_artifact(app_id, _base_payload(subject_id, ['TRUNCATE']))
        results = await handler.handle(artifact, session)

    assert results == []

    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_db_grant_handler_mixed_known_and_unknown(session_factory):
    """privileges=['SELECT', 'TRUNCATE', 'EXECUTE'] → two results (read, execute); TRUNCATE dropped."""
    _reset_registry_for_tests()
    register_handler('db_grant', DbGrantHandler())
    handler = get_handler('db_grant')
    assert handler is not None

    subject_id = uuid.uuid4()
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact = _make_artifact(app_id, _base_payload(subject_id, ['SELECT', 'TRUNCATE', 'EXECUTE']))
        results = await handler.handle(artifact, session)

    assert len(results) == 2
    slugs = {r.action_slug for r in results}
    assert slugs == {'read', 'execute'}

    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_db_grant_handler_invalid_payload_returns_empty(session_factory):
    """Missing required keys → [] (not an exception)."""
    _reset_registry_for_tests()
    register_handler('db_grant', DbGrantHandler())
    handler = get_handler('db_grant')
    assert handler is not None

    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact = _make_artifact(app_id, {'some_key': 'some_value'})
        results = await handler.handle(artifact, session)

    assert results == []
    _reset_registry_for_tests()
