# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for reconciliation engine actions (Phase 18 Step 9c).

Covers:
- Registration of both actions with correct metadata.
- Dispatch with invalid args raises ActionArgsValidationError.
- Happy-path dispatch for master_data_apply against a real DB fixture.
- Happy-path dispatch for run with mocked lake/catalog/events.
- Commit ownership: session.in_transaction() is True after run action dispatch.
"""

from __future__ import annotations

from collections.abc import Iterator
import importlib
import sys
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.reconciliation.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
    ReconciliationRunStatus,
)
from src.engines.reconciliation.repository import create_run
from src.platform.logs.service import LogService, noop_log_service
from src.platform.orchestrator.registry import (
    ACTION_REGISTRY,
    ActionArgsValidationError,
    ActionContext,
    RegisteredAction,
)

_ENGINE = 'reconciliation'
_ACTIONS_MODULE = 'src.engines.reconciliation.actions'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx_mock() -> ActionContext:
    """Build an ActionContext with a MagicMock session (no DB needed)."""
    return ActionContext(
        session=MagicMock(),
        log_service=cast(LogService, noop_log_service),
        pipeline_run_id=uuid.uuid4(),
        step_run_id=uuid.uuid4(),
        attempt=1,
        worker_id=None,
    )


def _make_ctx(session: AsyncSession) -> ActionContext:
    """Build an ActionContext with a real async session."""
    return ActionContext(
        session=session,
        log_service=cast(LogService, noop_log_service),
        pipeline_run_id=uuid.uuid4(),
        step_run_id=uuid.uuid4(),
        attempt=1,
        worker_id=None,
    )


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _registry_isolation() -> Iterator[None]:
    """Clear registry, re-import actions module to re-register, then clean up."""
    ACTION_REGISTRY._clear_for_tests()
    sys.modules.pop(_ACTIONS_MODULE, None)
    importlib.import_module(_ACTIONS_MODULE)
    yield
    ACTION_REGISTRY._clear_for_tests()


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


def test_both_actions_registered() -> None:
    """Both reconciliation.run and reconciliation.master_data_apply are registered."""
    run_rec: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, 'run')
    assert run_rec.engine == _ENGINE
    assert run_rec.action == 'run'
    assert run_rec.idempotent is True
    assert run_rec.args_schema is not None
    assert run_rec.result_schema is not None

    mda_rec: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, 'master_data_apply')
    assert mda_rec.engine == _ENGINE
    assert mda_rec.action == 'master_data_apply'
    assert mda_rec.idempotent is True
    assert mda_rec.args_schema is not None
    assert mda_rec.result_schema is not None


# ---------------------------------------------------------------------------
# Invalid-args tests — reconciliation.run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_args_missing_application_id() -> None:
    """run dispatch without application_id raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'run',
            raw_args={},
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_run_args_malformed_uuid() -> None:
    """run dispatch with malformed UUID raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'run',
            raw_args={'application_id': 'not-a-uuid'},
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_run_args_extra_field_forbidden() -> None:
    """run dispatch with unknown extra field raises ActionArgsValidationError (extra=forbid)."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'run',
            raw_args={'application_id': str(uuid.uuid4()), 'unknown_field': 'x'},
            ctx=ctx,
        )


# ---------------------------------------------------------------------------
# Invalid-args tests — reconciliation.master_data_apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_master_data_apply_args_access_fact_rejected() -> None:
    """master_data_apply dispatch with entity_type=access_fact raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'master_data_apply',
            raw_args={
                'run_id': str(uuid.uuid4()),
                'entity_type': 'access_fact',
            },
            ctx=ctx,
        )


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_application(session: AsyncSession) -> UUID:
    from src.platform.applications.models import Application  # noqa: PLC0415

    app = Application(
        name=f'test-app-{uuid.uuid4()}',
        code=f'app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


async def _seed_pending_apply_person_run(
    session: AsyncSession,
) -> tuple[UUID, UUID]:
    """Create a run in pending_apply status with one person CREATE delta item."""
    run = await create_run(
        session,
        application_id=None,
        entity_type=ReconciliationEntityType.person,
    )
    # Manually advance status to pending_apply (create_run starts at running)
    run.status = ReconciliationRunStatus.pending_apply
    await session.flush()

    item = ReconciliationDeltaItem(
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.person,
        operation=ReconciliationDeltaOperation.create,
        after_json={'external_id': f'EXT-ACTION-{uuid.uuid4().hex[:8]}', 'full_name': 'Action Test Person'},
    )
    session.add(item)
    await session.flush()
    return run.id, item.id


# ---------------------------------------------------------------------------
# Happy-path: master_data_apply (real DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_master_data_apply_happy_path(session_factory) -> None:  # type: ignore[no-untyped-def]
    """master_data_apply action applies a pending person CREATE item correctly."""
    import sqlalchemy as sa  # noqa: PLC0415
    from src.inventory.persons.models import Person  # noqa: PLC0415

    run_id: UUID
    async with session_factory() as session:
        run_id, _ = await _seed_pending_apply_person_run(session)
        # Fetch the external_id we used so we can verify the row later
        from src.engines.reconciliation.models import ReconciliationDeltaItem as _DI  # noqa: PLC0415

        row = await session.execute(sa.select(_DI).where(_DI.reconciliation_run_id == run_id))
        item = row.scalar_one()
        after = item.after_json or {}
        ext_id = after['external_id']
        await session.commit()

    async with session_factory() as session:
        ctx = _make_ctx(session)
        raw = await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'master_data_apply',
            raw_args={
                'run_id': str(run_id),
                'entity_type': 'person',
            },
            ctx=ctx,
        )
        # Runner owns commit; we commit here to make the insert visible
        await session.commit()

    assert raw['applied_count'] == 1
    assert raw['failed_count'] == 0
    assert raw['ignored_count'] == 0
    assert raw['run_id'] == str(run_id)
    assert raw['entity_type'] == 'person'

    # Verify Person row was created
    async with session_factory() as session:
        result = await session.execute(sa.select(Person).where(Person.external_id == ext_id))
        person = result.scalar_one()
        assert person.full_name == 'Action Test Person'


# ---------------------------------------------------------------------------
# Happy-path: run (mocked lake/catalog/events)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_happy_path_mocked_lake(session_factory) -> None:  # type: ignore[no-untyped-def]
    """run action returns ReconciliationRunResult envelope with run_id populated."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from src.engines.reconciliation.schemas import ReconciliationRunSummary  # noqa: PLC0415
    from src.platform.events.testing import CapturingEventService  # noqa: PLC0415

    async with session_factory() as session:
        app_id = await _seed_application(session)
        await session.commit()

    fake_summary = ReconciliationRunSummary(
        run_id=uuid.uuid4(),
        application_id=app_id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        artifacts_ingested=0,
        facts_created=0,
        facts_updated=0,
        facts_revoked=0,
        artifacts_unhandled=0,
        unchanged_count=0,
        observed_snapshot_id=None,
        current_snapshot_id=None,
    )

    capturing = CapturingEventService()
    fake_lake_session = MagicMock()
    fake_catalog = MagicMock()

    from src.platform.lake.config import LakeSettings  # noqa: PLC0415

    fake_settings = LakeSettings()

    async with session_factory() as session:
        ctx = _make_ctx(session)

        with (
            patch(
                'src.engines.reconciliation.actions.get_process_lake_session',
                new=AsyncMock(return_value=fake_lake_session),
            ),
            patch(
                'src.engines.reconciliation.actions.get_process_lake_catalog',
                return_value=fake_catalog,
            ),
            patch(
                'src.engines.reconciliation.actions.get_process_lake_settings',
                return_value=fake_settings,
            ),
            patch(
                'src.engines.reconciliation.actions._build_event_service',
                return_value=capturing,
            ),
            patch(
                'src.engines.reconciliation.service.run_reconciliation',
                new=AsyncMock(return_value=fake_summary),
            ),
        ):
            raw = await ACTION_REGISTRY.dispatch(
                _ENGINE,
                'run',
                raw_args={'application_id': str(app_id)},
                ctx=ctx,
            )

    assert raw['run_id'] == str(fake_summary.run_id)
    assert raw['application_id'] == str(app_id)
    assert 'facts_created' in raw
    assert 'started_at' in raw


# ---------------------------------------------------------------------------
# Commit ownership: session still in transaction after run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_does_not_commit_session(session_factory) -> None:  # type: ignore[no-untyped-def]
    """After run action dispatch, session.in_transaction() is still True (runner-owned)."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from src.engines.reconciliation.schemas import ReconciliationRunSummary  # noqa: PLC0415
    from src.platform.events.testing import CapturingEventService  # noqa: PLC0415

    async with session_factory() as session:
        app_id = await _seed_application(session)
        await session.commit()

    fake_summary = ReconciliationRunSummary(
        run_id=uuid.uuid4(),
        application_id=app_id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        artifacts_ingested=0,
        facts_created=0,
        facts_updated=0,
        facts_revoked=0,
        artifacts_unhandled=0,
        unchanged_count=0,
        observed_snapshot_id=None,
        current_snapshot_id=None,
    )

    from src.platform.lake.config import LakeSettings  # noqa: PLC0415

    async with session_factory() as session:
        ctx = _make_ctx(session)

        with (
            patch(
                'src.engines.reconciliation.actions.get_process_lake_session',
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                'src.engines.reconciliation.actions.get_process_lake_catalog',
                return_value=MagicMock(),
            ),
            patch(
                'src.engines.reconciliation.actions.get_process_lake_settings',
                return_value=LakeSettings(),
            ),
            patch(
                'src.engines.reconciliation.actions._build_event_service',
                return_value=CapturingEventService(),
            ),
            patch(
                'src.engines.reconciliation.service.run_reconciliation',
                new=AsyncMock(return_value=fake_summary),
            ),
        ):
            await ACTION_REGISTRY.dispatch(
                _ENGINE,
                'run',
                raw_args={'application_id': str(app_id)},
                ctx=ctx,
            )

        # Runner owns the commit — session must still be in an active transaction
        assert session.in_transaction()
