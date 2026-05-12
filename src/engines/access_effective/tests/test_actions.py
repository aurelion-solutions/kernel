# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for effective_access engine actions (Phase 18 Step 9a).

Covers:
- Registration of all three actions with correct metadata.
- Dispatch with invalid args raises ActionArgsValidationError.
- Happy-path dispatch against a real DB fixture.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
import importlib
import sys
from typing import cast
from unittest.mock import MagicMock
import uuid
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.engines.access_effective.models import EffectiveGrantEffect
from src.engines.access_effective.projector import EffectiveGrantDraft
from src.engines.access_effective.repository import upsert_effective_grants
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind
from src.platform.logs.service import LogService, noop_log_service
from src.platform.orchestrator.registry import (
    ACTION_REGISTRY,
    ActionArgsValidationError,
    ActionContext,
    RegisteredAction,
)

_ENGINE = 'access_effective'
_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_ACTIONS_MODULE = 'src.engines.access_effective.actions'


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
# DB seed helpers (mirrors test_routes.py pattern)
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


async def _seed_one_grant(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:
    """Seed one grant and return dict with ids for assertions."""
    async with session_factory() as session:
        sub_id = await _make_employee_subject(session)
        app_id, res_id = await _make_app_and_resource(session)
        fact_id = uuid.uuid4()
        init_id = await _make_initiative(session, fact_id)
        d = _draft(sub_id, SubjectKind.employee, app_id, res_id, fact_id, init_id)
        await upsert_effective_grants(session, [d])
        await session.flush()
        from sqlalchemy import text  # noqa: PLC0415

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
# Registration tests
# ---------------------------------------------------------------------------


def test_list_grants_action_registered() -> None:
    """effective_access.list_grants is registered with correct metadata."""
    record: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, 'list_grants')
    assert record.engine == _ENGINE
    assert record.action == 'list_grants'
    assert record.idempotent is True
    assert record.args_schema is not None
    assert record.result_schema is not None


def test_explain_access_action_registered() -> None:
    """effective_access.explain_access is registered with correct metadata."""
    record: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, 'explain_access')
    assert record.engine == _ENGINE
    assert record.action == 'explain_access'
    assert record.idempotent is True
    assert record.args_schema is not None
    assert record.result_schema is not None


def test_get_grant_action_registered() -> None:
    """effective_access.get_grant is registered with correct metadata."""
    record: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, 'get_grant')
    assert record.engine == _ENGINE
    assert record.action == 'get_grant'
    assert record.idempotent is True
    assert record.args_schema is not None
    assert record.result_schema is not None


# ---------------------------------------------------------------------------
# Invalid-args tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_grants_args_validation_missing_filter() -> None:
    """list_grants dispatch with no mandatory filter raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'list_grants',
            raw_args={},
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_list_grants_args_validation_limit_too_low() -> None:
    """list_grants dispatch with limit=0 raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'list_grants',
            raw_args={'subject_id': str(uuid.uuid4()), 'limit': 0},
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_list_grants_args_validation_limit_too_high() -> None:
    """list_grants dispatch with limit=1001 raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'list_grants',
            raw_args={'subject_id': str(uuid.uuid4()), 'limit': 1001},
            ctx=ctx,
        )


# ---------------------------------------------------------------------------
# Happy-path tests (DB required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_grants_dispatch_happy(session_factory) -> None:  # type: ignore[no-untyped-def]
    """list_grants returns seeded grant in result envelope."""
    ids = await _seed_one_grant(session_factory)
    async with session_factory() as session:
        ctx = _make_ctx(session)
        raw = await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'list_grants',
            raw_args={'subject_id': str(ids['subject_id']), 'active_only': False},
            ctx=ctx,
        )
    assert 'grants' in raw
    assert len(raw['grants']) >= 1
    g = raw['grants'][0]
    assert 'id' in g
    assert 'subject_id' in g
    assert 'effect' in g


@pytest.mark.asyncio
async def test_explain_access_dispatch_happy(session_factory) -> None:  # type: ignore[no-untyped-def]
    """explain_access returns correct effect and non-empty grants for seeded row."""
    ids = await _seed_one_grant(session_factory)
    async with session_factory() as session:
        ctx = _make_ctx(session)
        raw = await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'explain_access',
            raw_args={
                'subject_id': str(ids['subject_id']),
                'resource_id': str(ids['resource_id']),
                'action': 'read',
                'active_only': False,
            },
            ctx=ctx,
        )
    assert raw['effect'] in ('allow', 'deny', 'none')
    assert isinstance(raw['grants'], list)
    assert len(raw['grants']) >= 1


@pytest.mark.asyncio
async def test_get_grant_dispatch_found(session_factory) -> None:  # type: ignore[no-untyped-def]
    """get_grant returns non-None grant for a seeded id."""
    ids = await _seed_one_grant(session_factory)
    async with session_factory() as session:
        ctx = _make_ctx(session)
        raw = await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'get_grant',
            raw_args={'grant_id': str(ids['grant_id'])},
            ctx=ctx,
        )
    assert raw['grant'] is not None
    assert raw['grant']['id'] == str(ids['grant_id'])


@pytest.mark.asyncio
async def test_get_grant_dispatch_not_found(session_factory) -> None:  # type: ignore[no-untyped-def]
    """get_grant returns grant=None for a random UUID without raising."""
    async with session_factory() as session:
        ctx = _make_ctx(session)
        raw = await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'get_grant',
            raw_args={'grant_id': str(uuid.uuid4())},
            ctx=ctx,
        )
    assert raw['grant'] is None
