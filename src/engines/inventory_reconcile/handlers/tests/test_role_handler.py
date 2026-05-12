# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for reconciliation.handlers.role."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.engines.inventory_reconcile.handlers.role import RoleHandler
from src.engines.inventory_reconcile.registry import _reset_registry_for_tests, get_handler, register_handler
from src.inventory.accounts.models import Account
from src.inventory.subjects.models import Subject, SubjectKind

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _make_application(session) -> uuid.UUID:
    from src.platform.applications.models import Application

    app = Application(
        name=f'handler-test-{uuid.uuid4()}',
        code=f'ht-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


def _make_artifact(application_id: uuid.UUID, payload: dict, artifact_type: str = 'role'):
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


def _reg_role():
    """Register RoleHandler (registry must be empty before calling)."""
    register_handler('role', RoleHandler())


def _restore_production_registry() -> None:
    """Re-register all production handlers after a test reset.

    ``_reset_registry_for_tests()`` clears the global registry but Python does
    not re-execute module-level registration code on subsequent imports.  This
    helper explicitly re-registers every known handler so the registry is left
    in a production-equivalent state for subsequent tests (e.g. e2e tests that
    rely on the full registry being populated).
    """
    from src.engines.inventory_reconcile.handlers.acl_entry import AclEntryHandler  # noqa: PLC0415
    from src.engines.inventory_reconcile.handlers.db_grant import DbGrantHandler  # noqa: PLC0415
    from src.engines.inventory_reconcile.handlers.privilege import PrivilegeHandler  # noqa: PLC0415
    from src.engines.inventory_reconcile.handlers.sap_role import SapRoleHandler  # noqa: PLC0415

    _reset_registry_for_tests()
    register_handler('role', RoleHandler())
    register_handler('acl_entry', AclEntryHandler())
    register_handler('privilege', PrivilegeHandler())
    register_handler('db_grant', DbGrantHandler())
    register_handler('sap_role', SapRoleHandler())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_role_handler_registered_at_import():
    """RoleHandler can be registered and retrieved from the registry."""
    _reset_registry_for_tests()
    assert get_handler('role') is None

    # Registration is done by role.py at module level.
    # For test isolation we register a fresh instance directly.
    _reg_role()

    assert get_handler('role') is not None
    _restore_production_registry()


@pytest.mark.asyncio
async def test_role_handler_happy_path(session_factory):
    """Valid payload + Account in DB → single NormalizationResult with expected fields."""
    from src.inventory.employees.repository import create_employee  # noqa: PLC0415
    from src.inventory.persons.repository import create_person  # noqa: PLC0415

    _reset_registry_for_tests()
    _reg_role()
    handler = get_handler('role')
    assert handler is not None

    async with session_factory() as session:
        app_id = await _make_application(session)

        # Seed Subject (requires Person → Employee chain)
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
        subject_id = subj.id

        # Seed Account referencing the subject
        account = Account(application_id=app_id, username='alice', subject_id=subject_id)
        session.add(account)
        await session.flush()
        account_id = account.id

        payload = {
            'account_external_id': 'alice',
            'resource_key': 'my-resource',
            'resource_type': 'database',
            'action_slug': 'read',
            'effect': 'allow',
        }
        artifact = _make_artifact(app_id, payload)
        results = await handler.handle(artifact, session)

    assert len(results) == 1
    r = results[0]
    assert r.subject_id == subject_id
    assert r.account_id == account_id
    assert r.action_slug == 'read'
    assert r.effect == 'allow'
    assert r.resource_id is not None

    _restore_production_registry()


@pytest.mark.asyncio
async def test_role_handler_invalid_payload_returns_empty(session_factory):
    """Missing required keys → [] (not an exception)."""
    _reset_registry_for_tests()
    _reg_role()
    handler = get_handler('role')
    assert handler is not None

    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact = _make_artifact(app_id, {'some_key': 'some_value'})
        results = await handler.handle(artifact, session)

    assert results == []
    _restore_production_registry()


@pytest.mark.asyncio
async def test_role_handler_resource_reuse_no_duplicate(session_factory):
    """Calling handler twice with same (app_id, resource_type, resource_key) returns same resource_id."""
    from src.inventory.employees.repository import create_employee  # noqa: PLC0415
    from src.inventory.persons.repository import create_person  # noqa: PLC0415

    _reset_registry_for_tests()
    _reg_role()
    handler = get_handler('role')
    assert handler is not None

    async with session_factory() as session:
        app_id = await _make_application(session)

        # Seed Subject (requires Person → Employee chain)
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
        subject_id = subj.id

        # Seed Account referencing the subject
        account = Account(application_id=app_id, username='bob', subject_id=subject_id)
        session.add(account)
        await session.flush()

        payload = {
            'account_external_id': 'bob',
            'resource_key': 'shared-resource',
            'resource_type': 'database',
            'action_slug': 'read',
            'effect': 'allow',
        }

        artifact1 = _make_artifact(app_id, payload)
        artifact2 = _make_artifact(app_id, payload)
        results1 = await handler.handle(artifact1, session)
        results2 = await handler.handle(artifact2, session)

    assert len(results1) == 1
    assert len(results2) == 1
    assert results1[0].resource_id == results2[0].resource_id

    _restore_production_registry()
