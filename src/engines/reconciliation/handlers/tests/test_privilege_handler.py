# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for reconciliation.handlers.privilege."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.engines.reconciliation.handlers.privilege import PrivilegeHandler
from src.engines.reconciliation.registry import _reset_registry_for_tests, get_handler, register_handler

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _make_application(session) -> uuid.UUID:
    from src.platform.applications.models import Application

    app = Application(
        name=f'priv-handler-test-{uuid.uuid4()}',
        code=f'ph-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


def _make_artifact(application_id: uuid.UUID, payload: dict, artifact_type: str = 'privilege'):
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_privilege_handler_registered_at_import():
    """PrivilegeHandler can be registered and retrieved under 'privilege' key (not 'role')."""
    _reset_registry_for_tests()
    assert get_handler('privilege') is None
    assert get_handler('role') is None

    register_handler('privilege', PrivilegeHandler())
    assert get_handler('privilege') is not None
    assert get_handler('role') is None  # 'privilege' registration must not affect 'role'
    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_privilege_handler_happy_path(session_factory):
    """Valid privilege payload → single NormalizationResult with expected fields."""
    _reset_registry_for_tests()
    register_handler('privilege', PrivilegeHandler())
    handler = get_handler('privilege')
    assert handler is not None

    subject_id = uuid.uuid4()
    async with session_factory() as session:
        app_id = await _make_application(session)
        payload = {
            'subject_id': str(subject_id),
            'resource_key': 'my-resource',
            'resource_type': 'database',
            'action_slug': 'read',
            'effect': 'allow',
        }
        artifact = _make_artifact(app_id, payload)
        # DTO — no session.add needed
        results = await handler.handle(artifact, session)

    assert len(results) == 1
    r = results[0]
    assert r.subject_id == subject_id
    assert r.account_id is None
    assert r.action_slug == 'read'
    assert r.effect == 'allow'
    assert r.resource_id is not None

    _reset_registry_for_tests()


@pytest.mark.asyncio
async def test_privilege_handler_invalid_payload_returns_empty(session_factory):
    """Missing required keys → [] (not an exception)."""
    _reset_registry_for_tests()
    register_handler('privilege', PrivilegeHandler())
    handler = get_handler('privilege')
    assert handler is not None

    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact = _make_artifact(app_id, {'some_key': 'some_value'})
        results = await handler.handle(artifact, session)

    assert results == []
    _reset_registry_for_tests()
