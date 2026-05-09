# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests: pipeline skip-branch WARNING logs (Phase 17 Step 14)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.engines.reconciliation.contracts import NormalizationResult
from src.engines.reconciliation.pipeline import _phase_dispatch, _phase_resolve_action_ids
from src.engines.reconciliation.registry import _reset_registry_for_tests, register_handler
from src.engines.reconciliation.views import AccessArtifactRowView
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink


@pytest.fixture(autouse=True)
def reset_registry():
    _reset_registry_for_tests()
    yield
    _reset_registry_for_tests()


def _make_artifact(artifact_type: str = 'role') -> AccessArtifactRowView:
    """Build a minimal AccessArtifactRowView for testing."""
    from datetime import UTC, datetime

    return AccessArtifactRowView(
        id=uuid4(),
        application_id=uuid4(),
        artifact_type=artifact_type,
        external_id='ext-1',
        payload=None,
        raw_name='test-artifact',
        effect='allow',
        valid_from=None,
        valid_until=None,
        is_active=True,
        observed_at=datetime.now(UTC),
        ingest_batch_id=None,
    )


def _make_log_service() -> tuple[LogService, CapturingLogSink]:
    sink = CapturingLogSink()
    return LogService(sink=sink), sink


@pytest.mark.asyncio
async def test_skip_logs_on_handler_exception():
    """_phase_dispatch emits WARNING log with reason='handler_exception' when handler raises."""
    application_id = uuid4()
    artifact = _make_artifact(artifact_type='role')

    # Register a handler that raises
    class _RaisingHandler:
        async def handle(self, artifact: object, session: object) -> list[NormalizationResult]:
            raise RuntimeError('simulated handler failure')

    register_handler('role', _RaisingHandler())

    log_service, sink = _make_log_service()
    session_mock = AsyncMock()

    candidates, unhandled = await _phase_dispatch(
        session_mock,
        [artifact],
        logs=log_service,
        application_id=application_id,
        correlation_id=None,
    )

    # Artifact should be added as (artifact, None) sentinel
    assert len(candidates) == 1
    assert candidates[0][1] is None

    # Wait for fire-and-forget log emission
    import asyncio

    await asyncio.sleep(0)

    # Assert exactly one WARNING log captured
    warnings = [r for r in sink.records if r.level == LogLevel.WARNING]
    assert len(warnings) == 1, f'Expected 1 WARNING, got {len(warnings)}: {sink.records}'
    log = warnings[0]
    assert log.message == 'Reconciliation skipped artifact: handler raised'
    assert log.payload.get('reason') == 'handler_exception'
    assert log.payload.get('artifact_type') == 'role'
    assert log.payload.get('application_id') == str(application_id)
    assert log.payload.get('artifact_id') == str(artifact.id)


@pytest.mark.asyncio
async def test_skip_logs_on_unknown_action_slug():
    """_phase_resolve_action_ids emits WARNING with reason='unknown_action_slug'."""
    application_id = uuid4()
    artifact = _make_artifact()

    # Candidate with a slug that is NOT in ref_actions_local
    result = NormalizationResult(
        subject_id=uuid4(),
        account_id=None,
        resource_id=uuid4(),
        action_slug='nonexistent_slug',
        effect='allow',
        valid_from=None,
        valid_until=None,
    )
    candidates = [(artifact, result)]

    # Build a lake_session mock that returns empty rows for the slug lookup
    lake_session = MagicMock()
    lake_session.execute = MagicMock()
    lake_session.fetchall = MagicMock(return_value=[])

    log_service, sink = _make_log_service()

    resolved, errored = await _phase_resolve_action_ids(
        lake_session,
        candidates,
        logs=log_service,
        application_id=application_id,
        correlation_id=None,
    )

    assert errored == 1
    assert len(resolved) == 0

    # Wait for fire-and-forget log emission
    import asyncio

    await asyncio.sleep(0)

    warnings = [r for r in sink.records if r.level == LogLevel.WARNING]
    assert len(warnings) == 1, f'Expected 1 WARNING, got {len(warnings)}: {sink.records}'
    log = warnings[0]
    assert log.message == 'Reconciliation skipped candidate: unknown action slug'
    assert log.payload.get('reason') == 'unknown_action_slug'
    assert log.payload.get('action_slug') == 'nonexistent_slug'
    assert log.payload.get('application_id') == str(application_id)
    assert log.payload.get('artifact_id') == str(artifact.id)
