# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for effective_access projection engine actions (Phase 18 Step 9b).

Covers:
- Registration of all three projection actions with idempotent=False.
- Dispatch with invalid args raises ActionArgsValidationError.
- Happy-path dispatch against a real DB fixture.
- Commit-ownership guard: session.commit() is never called inside the action body.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
import importlib
import sys
from typing import cast
from unittest.mock import AsyncMock, patch
import uuid
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind
from src.platform.logs.service import LogService, noop_log_service
from src.platform.orchestrator.registry import (
    ACTION_REGISTRY,
    ActionArgsValidationError,
    ActionContext,
    RegisteredAction,
)

_ENGINE = 'effective_access'
_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_ACTIONS_MODULE = 'src.engines.effective_access.actions'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx_mock() -> ActionContext:
    """Build an ActionContext with a MagicMock session (no DB needed)."""
    from unittest.mock import MagicMock  # noqa: PLC0415

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
# DB seed helpers
# ---------------------------------------------------------------------------


async def _make_employee_subject(session: AsyncSession) -> UUID:
    from src.inventory.employees.repository import create_employee  # noqa: PLC0415
    from src.inventory.persons.repository import create_person  # noqa: PLC0415
    from src.inventory.subjects.models import Subject  # noqa: PLC0415

    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
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
    from src.inventory.resources.models import Resource  # noqa: PLC0415
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
    res_ext = str(uuid.uuid4())
    resource = Resource(
        external_id=res_ext,
        application_id=app.id,
        kind='database',
        resource_type='database',
        resource_key=res_ext,
    )
    session.add(resource)
    await session.flush()
    return app.id, resource.id


async def _seed_access_fact_in_shim(
    session: AsyncSession,
    subject_id: UUID,
    resource_id: UUID,
) -> UUID:
    """Insert a row into the access_facts shim table (no Iceberg required).

    The shim table exists for exactly this use case — legacy tests that need a
    fact to exist in PG for JOIN-based reads.  The ``action_id`` references
    ``ref_actions`` which is seeded by the test fixture.
    """
    import sqlalchemy as sa  # noqa: PLC0415

    fact_id = uuid.uuid4()
    # Resolve action_id for slug='read' from ref_actions (seeded by conftest)
    row = await session.execute(
        sa.text("SELECT id FROM ref_actions WHERE slug = 'read'"),
    )
    action_id = row.scalar_one()
    await session.execute(
        sa.text(
            'INSERT INTO access_facts '
            '(id, subject_id, resource_id, action_id, effect, valid_from, observed_at) '
            "VALUES (:id, :sid, :rid, :aid, 'allow', :vf, :oa)"
        ),
        {
            'id': fact_id,
            'sid': subject_id,
            'rid': resource_id,
            'aid': action_id,
            'vf': _NOW,
            'oa': _NOW,
        },
    )
    await session.flush()
    return fact_id


async def _make_initiative(session: AsyncSession, access_fact_id: UUID) -> UUID:
    from src.inventory.initiatives.models import Initiative  # noqa: PLC0415

    init = Initiative(
        access_fact_id=access_fact_id,
        type=InitiativeType.birthright,
        origin='test-origin',
        valid_from=_NOW,
    )
    session.add(init)
    await session.flush()
    return init.id


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


def test_project_access_fact_registered() -> None:
    """effective_access.project_access_fact is registered with idempotent=False."""
    record: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, 'project_access_fact')
    assert record.engine == _ENGINE
    assert record.action == 'project_access_fact'
    assert record.idempotent is False
    assert record.args_schema is not None
    assert record.result_schema is not None


def test_project_application_registered() -> None:
    """effective_access.project_application is registered with idempotent=True.

    Service uses UPSERT into effective_grants — safe to retry on failure.
    Flipped from False to True in Phase 18 Step 21 per ARCH_CONTEXT §355 mandate.
    """
    record: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, 'project_application')
    assert record.engine == _ENGINE
    assert record.action == 'project_application'
    assert record.idempotent is True
    assert record.args_schema is not None
    assert record.result_schema is not None


def test_apply_incremental_change_registered() -> None:
    """effective_access.apply_incremental_change is registered with idempotent=False."""
    record: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, 'apply_incremental_change')
    assert record.engine == _ENGINE
    assert record.action == 'apply_incremental_change'
    assert record.idempotent is False
    assert record.args_schema is not None
    assert record.result_schema is not None


# ---------------------------------------------------------------------------
# Invalid-args tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_access_fact_missing_access_fact_id() -> None:
    """project_access_fact dispatch without access_fact_id raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'project_access_fact',
            raw_args={'now': _NOW.isoformat()},
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_project_access_fact_missing_now() -> None:
    """project_access_fact dispatch without now raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'project_access_fact',
            raw_args={'access_fact_id': str(uuid.uuid4())},
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_project_application_missing_application_id() -> None:
    """project_application dispatch without application_id raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'project_application',
            raw_args={'now': _NOW.isoformat()},
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_apply_incremental_change_upsert_without_access_fact_id() -> None:
    """apply_incremental_change UPSERT without access_fact_id raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'apply_incremental_change',
            raw_args={
                'change_kind': 'upsert',
                'observed_at': _NOW.isoformat(),
            },
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_apply_incremental_change_invalidate_initiative_without_initiative_id() -> None:
    """apply_incremental_change INVALIDATE_INITIATIVE without initiative_id raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'apply_incremental_change',
            raw_args={
                'change_kind': 'invalidate_initiative',
                'observed_at': _NOW.isoformat(),
            },
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_apply_incremental_change_extra_field_forbidden() -> None:
    """apply_incremental_change dispatch with unknown field raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'apply_incremental_change',
            raw_args={
                'change_kind': 'upsert',
                'observed_at': _NOW.isoformat(),
                'access_fact_id': str(uuid.uuid4()),
                'not_a_real_field': 'x',
            },
            ctx=ctx,
        )


# ---------------------------------------------------------------------------
# Happy-path tests (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_access_fact_not_found_raises(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """project_access_fact raises AccessFactNotFoundError for a non-existent fact id."""
    from src.engines.effective_access.service import AccessFactNotFoundError  # noqa: PLC0415

    async with session_factory() as session:
        ctx = _make_ctx(session)
        fact_id = uuid.uuid4()
        with pytest.raises(AccessFactNotFoundError):
            await ACTION_REGISTRY.dispatch(
                _ENGINE,
                'project_access_fact',
                raw_args={
                    'access_fact_id': str(fact_id),
                    'now': _NOW.isoformat(),
                },
                ctx=ctx,
            )


@pytest.mark.asyncio
async def test_project_application_empty_happy(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """project_application on an application with no facts returns 0 pairs (valid success)."""
    async with session_factory() as session:
        from src.platform.applications.models import Application  # noqa: PLC0415

        app = Application(
            name=f'empty-app-{uuid.uuid4()}',
            code=f'emp-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        ctx = _make_ctx(session)
        raw = await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'project_application',
            raw_args={
                'application_id': str(app_id),
                'now': _NOW.isoformat(),
            },
            ctx=ctx,
        )

    assert raw['pairs_projected'] == 0
    assert raw['rows_upserted'] == 0
    assert raw['scope_id'] == str(app_id)
    assert raw['scope_kind'] == 'application'


@pytest.mark.asyncio
async def test_apply_incremental_change_invalidate_fact_happy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """apply_incremental_change INVALIDATE_FACT on a missing fact returns tombstoned=0 (not an error)."""
    async with session_factory() as session:
        ctx = _make_ctx(session)
        fact_id = uuid.uuid4()
        raw = await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'apply_incremental_change',
            raw_args={
                'change_kind': 'invalidate_fact',
                'observed_at': _NOW.isoformat(),
                'access_fact_id': str(fact_id),
            },
            ctx=ctx,
        )

    assert raw['rows_tombstoned'] == 0
    assert raw['scope_kind'] == 'access_fact'
    assert raw['scope_id'] == str(fact_id)


@pytest.mark.asyncio
async def test_apply_incremental_change_invalidate_initiative_happy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """apply_incremental_change INVALIDATE_INITIATIVE on a missing initiative returns tombstoned=0 (no-op)."""
    async with session_factory() as session:
        ctx = _make_ctx(session)
        initiative_id = uuid.uuid4()
        raw = await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'apply_incremental_change',
            raw_args={
                'change_kind': 'invalidate_initiative',
                'observed_at': _NOW.isoformat(),
                'initiative_id': str(initiative_id),
            },
            ctx=ctx,
        )

    assert raw['rows_tombstoned'] == 0
    assert raw['scope_kind'] == 'initiative'
    assert raw['scope_id'] == str(initiative_id)


# ---------------------------------------------------------------------------
# Commit-ownership guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_application_no_commit(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """session.commit() is never called inside the project_application action body."""
    async with session_factory() as session:
        from src.platform.applications.models import Application  # noqa: PLC0415

        app = Application(
            name=f'nc-app-{uuid.uuid4()}',
            code=f'nc-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        ctx = _make_ctx(session)
        with patch.object(session, 'commit', new_callable=AsyncMock) as mock_commit:
            await ACTION_REGISTRY.dispatch(
                _ENGINE,
                'project_application',
                raw_args={
                    'application_id': str(app_id),
                    'now': _NOW.isoformat(),
                },
                ctx=ctx,
            )
            mock_commit.assert_not_called()
