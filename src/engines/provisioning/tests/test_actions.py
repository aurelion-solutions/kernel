# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for provisioning engine actions (Phase 18 Step 9e).

Covers:
- Registration of provisioning.create_account and provisioning.delete_account
  with correct metadata (idempotent=True, correct schemas).
- Dispatch with invalid args raises ActionArgsValidationError.
- Happy-path dispatch via DummyConnectorClient.
- Commit ownership: session.in_transaction() is True after dispatch.
- Side-effect import via src.engines.provisioning __init__.py.
"""

from __future__ import annotations

from collections.abc import Iterator
import importlib
import sys
from typing import cast
from unittest.mock import MagicMock, patch
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.provisioning.actions import (
    CreateAccountArgs,
    CreateAccountResult,
    DeleteAccountArgs,
    DeleteAccountResult,
)
from src.platform.logs.service import LogService, noop_log_service
from src.platform.orchestrator.registry import (
    ACTION_REGISTRY,
    ActionArgsValidationError,
    ActionContext,
    RegisteredAction,
)

_ENGINE = 'provisioning'
_CREATE_ACTION = 'create_account'
_DELETE_ACTION = 'delete_account'
_ACTIONS_MODULE = 'src.engines.provisioning.actions'


# ---------------------------------------------------------------------------
# Stub connector
# ---------------------------------------------------------------------------


class DummyConnectorClient:
    """Stub connector that returns a minimal ok response."""

    async def invoke(
        self,
        _instance_id: str,
        operation: str,
        payload: dict,
        *,
        result_storage_requested: bool = False,
        **_kwargs,
    ) -> dict:
        return {'status': 'ok', 'payload': {}}


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
    """Clear registry, re-import actions module to re-register, then clean up.

    Mirrors the ``_restore_action_registry`` fixture in
    ``src/platform/orchestrator/tests/test_routes.py``:
    if ``test_no_side_effects_on_import`` (test_registry.py) has run
    ``importlib.reload(registry_module)``, the module-level ``ACTION_REGISTRY``
    in this file points at the old singleton while the module in sys.modules
    holds a new one.  We repair the binding so that the side-effect import
    registers into the same singleton our dispatch calls use.

    Also patches the module-level ``ActionArgsValidationError`` binding to
    stay in sync with the (possibly reloaded) registry module so that
    ``pytest.raises(ActionArgsValidationError)`` catches errors raised by
    ``dispatch``.
    """
    import src.engines.provisioning.tests.test_actions as _self_mod  # noqa: PLC0415
    import src.platform.orchestrator.registry as _reg_mod  # noqa: PLC0415

    # Repair the module-level binding if reload() replaced the singleton.
    if _reg_mod.ACTION_REGISTRY is not ACTION_REGISTRY:
        _reg_mod.ACTION_REGISTRY = ACTION_REGISTRY

    # Keep the module-level error class in sync so pytest.raises catches it.
    _self_mod.ActionArgsValidationError = _reg_mod.ActionArgsValidationError

    ACTION_REGISTRY._clear_for_tests()
    sys.modules.pop(_ACTIONS_MODULE, None)
    importlib.import_module(_ACTIONS_MODULE)
    yield
    ACTION_REGISTRY._clear_for_tests()


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_app_and_connector(session: AsyncSession) -> uuid.UUID:
    """Seed an Application with an online ConnectorInstance and return app.id."""
    from src.platform.applications.models import Application  # noqa: PLC0415
    from src.platform.connectors.service import ConnectorInstanceService  # noqa: PLC0415

    connector_service = ConnectorInstanceService()
    instance_id = f'prov-test-inst-{uuid.uuid4().hex[:8]}'
    await connector_service.upsert_instance(
        session,
        instance_id=instance_id,
        tags=['provisioning-test'],
    )

    app = Application(
        name=f'prov-test-app-{uuid.uuid4().hex[:8]}',
        code=f'prov-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=['provisioning-test'],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


def test_create_account_action_registered() -> None:
    """provisioning.create_account is registered with idempotent=True and correct schemas."""
    rec: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, _CREATE_ACTION)
    assert rec.idempotent is True
    assert rec.args_schema.__name__ == CreateAccountArgs.__name__
    assert rec.result_schema.__name__ == CreateAccountResult.__name__
    assert rec.engine == _ENGINE
    assert rec.action == _CREATE_ACTION


def test_delete_account_action_registered() -> None:
    """provisioning.delete_account is registered with idempotent=True and correct schemas."""
    rec: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, _DELETE_ACTION)
    assert rec.idempotent is True
    assert rec.args_schema.__name__ == DeleteAccountArgs.__name__
    assert rec.result_schema.__name__ == DeleteAccountResult.__name__
    assert rec.engine == _ENGINE
    assert rec.action == _DELETE_ACTION


# ---------------------------------------------------------------------------
# Invalid-args tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_missing_username() -> None:
    """create_account without username raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            _CREATE_ACTION,
            raw_args={'application_id': str(uuid.uuid4())},
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_create_username_too_long() -> None:
    """create_account with username >255 chars raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            _CREATE_ACTION,
            raw_args={
                'application_id': str(uuid.uuid4()),
                'username': 'x' * 256,
            },
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_create_email_too_long() -> None:
    """create_account with email >255 chars raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            _CREATE_ACTION,
            raw_args={
                'application_id': str(uuid.uuid4()),
                'username': 'alice',
                'email': 'a' * 256,
            },
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_create_extra_field_rejected() -> None:
    """create_account with extra field raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            _CREATE_ACTION,
            raw_args={
                'application_id': str(uuid.uuid4()),
                'username': 'alice',
                'unexpected_field': 'oops',
            },
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_delete_missing_username() -> None:
    """delete_account without username raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            _DELETE_ACTION,
            raw_args={'application_id': str(uuid.uuid4())},
            ctx=ctx,
        )


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_account_happy_path(session_factory) -> None:  # type: ignore[no-untyped-def]
    """create_account action returns {username, email, status='accepted'}."""
    dummy = DummyConnectorClient()

    async with session_factory() as session:
        app_id = await _seed_app_and_connector(session)
        await session.commit()

    async with session_factory() as session:
        ctx = _make_ctx(session)
        with patch(
            'src.engines.provisioning.actions._build_connector_client',
            return_value=dummy,
        ):
            result = await ACTION_REGISTRY.dispatch(
                _ENGINE,
                _CREATE_ACTION,
                raw_args={
                    'application_id': str(app_id),
                    'username': 'alice',
                    'email': 'alice@example.com',
                },
                ctx=ctx,
            )

    assert result['username'] == 'alice'
    assert result['email'] == 'alice@example.com'
    assert result['status'] == 'accepted'


@pytest.mark.asyncio
async def test_delete_account_happy_path(session_factory) -> None:  # type: ignore[no-untyped-def]
    """delete_account action returns {status: 'accepted'}."""
    dummy = DummyConnectorClient()

    async with session_factory() as session:
        app_id = await _seed_app_and_connector(session)
        await session.commit()

    async with session_factory() as session:
        ctx = _make_ctx(session)
        with patch(
            'src.engines.provisioning.actions._build_connector_client',
            return_value=dummy,
        ):
            result = await ACTION_REGISTRY.dispatch(
                _ENGINE,
                _DELETE_ACTION,
                raw_args={
                    'application_id': str(app_id),
                    'username': 'alice',
                },
                ctx=ctx,
            )

    assert result['status'] == 'accepted'


# ---------------------------------------------------------------------------
# Commit-ownership test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provisioning_actions_do_not_commit_session(session_factory) -> None:  # type: ignore[no-untyped-def]
    """After dispatch, session.in_transaction() is still True (runner-owned)."""
    dummy = DummyConnectorClient()

    async with session_factory() as session:
        app_id = await _seed_app_and_connector(session)
        await session.commit()

    async with session_factory() as session:
        ctx = _make_ctx(session)
        with patch(
            'src.engines.provisioning.actions._build_connector_client',
            return_value=dummy,
        ):
            await ACTION_REGISTRY.dispatch(
                _ENGINE,
                _DELETE_ACTION,
                raw_args={
                    'application_id': str(app_id),
                    'username': 'bob',
                },
                ctx=ctx,
            )

        # Runner owns the commit — session must still be in an active transaction
        assert session.in_transaction()


# ---------------------------------------------------------------------------
# Side-effect import test
# ---------------------------------------------------------------------------


def test_slice_init_registers_actions() -> None:
    """Importing src.engines.provisioning triggers action registration."""
    ACTION_REGISTRY._clear_for_tests()
    # Remove cached modules so the import re-triggers side effects
    for mod in list(sys.modules):
        if mod.startswith('src.engines.provisioning'):
            sys.modules.pop(mod, None)

    importlib.import_module('src.engines.provisioning')

    assert ACTION_REGISTRY.get(_ENGINE, _CREATE_ACTION) is not None
    assert ACTION_REGISTRY.get(_ENGINE, _DELETE_ACTION) is not None
