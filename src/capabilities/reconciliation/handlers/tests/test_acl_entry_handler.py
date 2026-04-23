# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for reconciliation.handlers.acl_entry."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.capabilities.reconciliation.handlers.acl_entry import AclEntryHandler
from src.capabilities.reconciliation.registry import _reset_registry_for_tests, get_handler, register_handler

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _make_application(session) -> uuid.UUID:
    from src.platform.applications.models import Application

    app = Application(
        name=f'acl-handler-test-{uuid.uuid4()}',
        code=f'ah-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


def _make_artifact(application_id: uuid.UUID, payload: dict, artifact_type: str = 'acl_entry'):
    from src.inventory.access_artifacts.models import AccessArtifact

    return AccessArtifact(
        id=uuid.uuid4(),
        application_id=application_id,
        artifact_type=artifact_type,
        external_id=str(uuid.uuid4()),
        payload=payload,
        observed_at=datetime.now(UTC),
        is_active=True,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_acl_entry_handler_registered_at_import():
    """AclEntryHandler can be registered and retrieved from the registry."""
    _reset_registry_for_tests()
    assert get_handler('acl_entry') is None

    register_handler('acl_entry', AclEntryHandler())
    assert get_handler('acl_entry') is not None
    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_acl_entry_handler_allow_happy_path(session_factory):
    """Valid ACL payload with effect='allow' → single NormalizationResult."""
    _reset_registry_for_tests()
    register_handler('acl_entry', AclEntryHandler())
    handler = get_handler('acl_entry')
    assert handler is not None

    subject_id = uuid.uuid4()
    async with session_factory() as session:
        app_id = await _make_application(session)
        payload = {
            'subject_id': str(subject_id),
            'resource_type': 'folder',
            'resource_key': '/finance',
            'action_slug': 'read',
            'effect': 'allow',
        }
        artifact = _make_artifact(app_id, payload)
        session.add(artifact)
        await session.flush()

        results = await handler.handle(artifact, session)

    assert len(results) == 1
    r = results[0]
    assert r.subject_id == subject_id
    assert r.action_slug == 'read'
    assert r.effect == 'allow'
    assert r.resource_id is not None

    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_acl_entry_handler_deny_effect_passed_through(session_factory):
    """effect='deny' is passed through verbatim — no normalization inside handler."""
    _reset_registry_for_tests()
    register_handler('acl_entry', AclEntryHandler())
    handler = get_handler('acl_entry')
    assert handler is not None

    subject_id = uuid.uuid4()
    async with session_factory() as session:
        app_id = await _make_application(session)
        payload = {
            'subject_id': str(subject_id),
            'resource_type': 'folder',
            'resource_key': '/finance-deny',
            'action_slug': 'write',
            'effect': 'deny',
        }
        artifact = _make_artifact(app_id, payload)
        session.add(artifact)
        await session.flush()

        results = await handler.handle(artifact, session)

    assert len(results) == 1
    assert results[0].effect == 'deny'

    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_acl_entry_handler_invalid_payload_returns_empty(session_factory):
    """Missing required keys → [] (not an exception)."""
    _reset_registry_for_tests()
    register_handler('acl_entry', AclEntryHandler())
    handler = get_handler('acl_entry')
    assert handler is not None

    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact = _make_artifact(app_id, {'some_key': 'some_value'})
        session.add(artifact)
        await session.flush()

        results = await handler.handle(artifact, session)

    assert results == []
    _reset_registry_for_tests()
