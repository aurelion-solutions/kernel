# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for capabilities.reconciliation.pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.capabilities.reconciliation.contracts import NormalizationResult
from src.capabilities.reconciliation.pipeline import run_reconciliation
from src.capabilities.reconciliation.registry import _reset_registry_for_tests, register_handler
from src.inventory.access_facts.models import AccessFactEffect
from src.inventory.access_facts.service import AccessFactService
from src.inventory.artifact_bindings.service import ArtifactBindingService
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService

_NOW = datetime(2026, 1, 1, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def access_fact_service(event_service: EventService) -> AccessFactService:
    return AccessFactService(event_service=event_service)


@pytest.fixture
def artifact_binding_service(event_service: EventService) -> ArtifactBindingService:
    return ArtifactBindingService(event_service=event_service)


@pytest.fixture(autouse=True)
def reset_registry():
    _reset_registry_for_tests()
    yield
    _reset_registry_for_tests()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _make_application(session) -> uuid.UUID:
    from src.platform.applications.models import Application

    app = Application(
        name=f'pipe-test-{uuid.uuid4()}',
        code=f'pt-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


async def _make_subject(session) -> uuid.UUID:
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.subjects.models import Subject, SubjectKind

    person = await create_person(session, external_id=str(uuid.uuid4()), description='test')
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


async def _make_resource(session, app_id: uuid.UUID) -> uuid.UUID:
    from src.inventory.resources.models import Resource

    ext = str(uuid.uuid4())
    res = Resource(
        external_id=ext,
        application_id=app_id,
        kind='database',
        resource_type='database',
        resource_key=ext,
    )
    session.add(res)
    await session.flush()
    return res.id


async def _resolve_action_id(session, slug: str) -> int:
    from sqlalchemy import select
    from src.inventory.actions.models import Action as RefAction

    result = await session.execute(select(RefAction.id).where(RefAction.slug == slug))
    val = result.scalar_one_or_none()
    assert val is not None, f'Action slug {slug!r} not seeded'
    return val


async def _seed_artifact(
    session,
    application_id: uuid.UUID,
    artifact_type: str,
    payload: dict,
) -> uuid.UUID:
    from src.inventory.access_artifacts.models import AccessArtifact

    art = AccessArtifact(
        application_id=application_id,
        artifact_type=artifact_type,
        external_id=str(uuid.uuid4()),
        payload=payload,
        observed_at=datetime.now(UTC),
        is_active=True,
    )
    session.add(art)
    await session.flush()
    return art.id


async def _seed_fact(
    session,
    subject_id: uuid.UUID,
    resource_id: uuid.UUID,
    action_id: int,
    effect: AccessFactEffect = AccessFactEffect.allow,
) -> uuid.UUID:
    """Insert an active AccessFact directly into DB (bypass service for state setup)."""
    from src.inventory.access_facts.models import AccessFact

    fact = AccessFact(
        subject_id=subject_id,
        account_id=None,
        resource_id=resource_id,
        action_id=action_id,
        effect=effect,
        observed_at=datetime.now(UTC),
        is_active=True,
    )
    session.add(fact)
    await session.flush()
    return fact.id


# ---------------------------------------------------------------------------
# Dummy handler factory
# ---------------------------------------------------------------------------


def _make_handler(results_factory):
    """Create a simple handler whose handle() returns results_factory(artifact)."""

    class _H:
        async def handle(self, artifact, session):
            return results_factory(artifact)

    return _H()


def _register_role_handler() -> None:
    """Register the role handler safely for tests.

    With ``--import-mode=importlib`` (the project default), importing
    ``handlers.role`` causes ``handlers/__init__.py`` to re-execute and call
    ``register_handler('role', ...)`` as a side-effect.  We therefore:
    1. Trigger the import (which may register via the side-effect).
    2. Reset the registry.
    3. Register a fresh instance explicitly.

    This guarantees exactly one ``RoleHandler`` instance is in the registry
    and the test starts from a known state.
    """
    from src.capabilities.reconciliation.handlers.role import RoleHandler

    _reset_registry_for_tests()
    register_handler('role', RoleHandler())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_application_no_artifacts_no_revoke(
    session_factory,
    access_fact_service,
    artifact_binding_service,
):
    """Zero artifacts + zero active facts → all counters zero."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert summary.artifacts_ingested == 0
    assert summary.facts_created == 0
    assert summary.facts_updated == 0
    assert summary.facts_revoked == 0
    assert summary.artifacts_unhandled == 0


@pytest.mark.asyncio
async def test_single_role_artifact_creates_fact(
    session_factory,
    access_fact_service,
    artifact_binding_service,
    capturing_events,
):
    """One 'role' artifact → handler registered → one AccessFact created, one binding."""
    # Register role handler (re-import after reset)
    _register_role_handler()

    async with session_factory() as session:
        app_id = await _make_application(session)
        subject_id = await _make_subject(session)

        payload = {
            'subject_id': str(subject_id),
            'resource_key': 'db-1',
            'resource_type': 'database',
            'action_slug': 'read',
            'effect': 'allow',
        }
        await _seed_artifact(session, app_id, 'role', payload)
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert summary.facts_created == 1
    assert summary.artifacts_unhandled == 0
    assert summary.artifacts_ingested == 1

    created = capturing_events.filter_by_type('inventory.access_fact.created')
    assert len(created) == 1

    binding_events = capturing_events.filter_by_type('inventory.artifact_binding.created')
    assert len(binding_events) == 1


@pytest.mark.asyncio
async def test_unhandled_artifact_type_increments_counter(
    session_factory,
    access_fact_service,
    artifact_binding_service,
):
    """Artifact with unknown artifact_type → unhandled counter increments, no fact."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        await _seed_artifact(session, app_id, 'sap_role', {'key': 'val'})
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert summary.artifacts_unhandled == 1
    assert summary.facts_created == 0


@pytest.mark.asyncio
async def test_set_diff_revokes_removed_facts(
    session_factory,
    access_fact_service,
    artifact_binding_service,
    capturing_events,
):
    """3 active facts, 0 new artifacts → 3 revoked."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        subject_id = await _make_subject(session)
        action_id = await _resolve_action_id(session, 'read')

        for _ in range(3):
            res_id = await _make_resource(session, app_id)
            await _seed_fact(session, subject_id, res_id, action_id)
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert summary.facts_revoked == 3
    assert summary.facts_created == 0

    revoked_events = capturing_events.filter_by_type('inventory.access_fact.revoked')
    assert len(revoked_events) == 3


@pytest.mark.asyncio
async def test_set_diff_reactivates_revoked_fact(
    session_factory,
    access_fact_service,
    artifact_binding_service,
    capturing_events,
):
    """Revoked fact matching new artifact → reactivated, counted under facts_created."""
    _register_role_handler()

    async with session_factory() as session:
        app_id = await _make_application(session)
        subject_id = await _make_subject(session)
        action_id = await _resolve_action_id(session, 'read')

        # Create and revoke a fact
        res_id = await _make_resource(session, app_id)
        await session.flush()

        from src.inventory.resources.repository import get_resource_by_id

        resource = await get_resource_by_id(session, res_id)
        assert resource is not None

        fact_id = await _seed_fact(session, subject_id, res_id, action_id)
        # Manually revoke it
        from src.inventory.access_facts.repository import get_access_fact_by_id, revoke_access_fact

        fact = await get_access_fact_by_id(session, fact_id)
        await revoke_access_fact(session, fact, revoked_at=datetime.now(UTC))

        # Seed an artifact that should reactivate the revoked fact
        payload = {
            'subject_id': str(subject_id),
            'resource_key': resource.resource_key,
            'resource_type': resource.resource_type,
            'action_slug': 'read',
            'effect': 'allow',
        }
        await _seed_artifact(session, app_id, 'role', payload)
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    # Reactivation counts under facts_created
    assert summary.facts_created == 1
    reactivated = capturing_events.filter_by_type('inventory.access_fact.reactivated')
    assert len(reactivated) == 1


@pytest.mark.asyncio
async def test_set_diff_updates_fields_on_common_key(
    session_factory,
    access_fact_service,
    artifact_binding_service,
    capturing_events,
):
    """Active fact with effect='allow', new artifact with effect='deny' → field updated."""
    _register_role_handler()

    async with session_factory() as session:
        app_id = await _make_application(session)
        subject_id = await _make_subject(session)
        action_id = await _resolve_action_id(session, 'read')

        res_id = await _make_resource(session, app_id)
        await session.flush()

        from src.inventory.resources.repository import get_resource_by_id

        resource = await get_resource_by_id(session, res_id)

        # Seed active fact with 'allow'
        await _seed_fact(session, subject_id, res_id, action_id, AccessFactEffect.allow)

        # Seed artifact with 'deny' (same natural key)
        payload = {
            'subject_id': str(subject_id),
            'resource_key': resource.resource_key,
            'resource_type': resource.resource_type,
            'action_slug': 'read',
            'effect': 'deny',
        }
        await _seed_artifact(session, app_id, 'role', payload)
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert summary.facts_updated == 1
    assert summary.facts_created == 0
    updated_events = capturing_events.filter_by_type('inventory.access_fact.updated')
    assert len(updated_events) == 1


@pytest.mark.asyncio
async def test_delta_scope_respects_application_id(
    session_factory,
    access_fact_service,
    artifact_binding_service,
):
    """Run on app A must not revoke facts of app B."""
    _register_role_handler()

    async with session_factory() as session:
        app_a = await _make_application(session)
        app_b = await _make_application(session)
        subject_id = await _make_subject(session)
        action_id = await _resolve_action_id(session, 'read')

        # 1 active fact in app B — must not be touched
        res_b = await _make_resource(session, app_b)
        await _seed_fact(session, subject_id, res_b, action_id)
        await session.flush()

        # Run on app A (no artifacts → empty)
        summary = await run_reconciliation(
            session,
            application_id=app_a,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    # App A has no facts → nothing revoked for A; app B fact untouched
    assert summary.facts_revoked == 0


@pytest.mark.asyncio
async def test_empty_handler_result_is_not_unhandled(
    session_factory,
    access_fact_service,
    artifact_binding_service,
):
    """Handler registered but returns [] → artifacts_unhandled stays 0."""
    register_handler('empty_type', _make_handler(lambda _: []))

    async with session_factory() as session:
        app_id = await _make_application(session)
        await _seed_artifact(session, app_id, 'empty_type', {})
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert summary.artifacts_unhandled == 0
    assert summary.facts_created == 0


@pytest.mark.asyncio
async def test_completed_event_payload(
    session_factory,
    access_fact_service,
    artifact_binding_service,
):
    """ReconciliationRunSummary has all eight fields with correct types."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert isinstance(summary.application_id, uuid.UUID)
    assert isinstance(summary.started_at, datetime)
    assert isinstance(summary.finished_at, datetime)
    assert summary.started_at.tzinfo is not None
    assert summary.finished_at.tzinfo is not None
    for field in ('artifacts_ingested', 'facts_created', 'facts_updated', 'facts_revoked', 'artifacts_unhandled'):
        assert getattr(summary, field) >= 0


@pytest.mark.asyncio
async def test_unknown_action_slug_counts_errored_not_created(
    session_factory,
    access_fact_service,
    artifact_binding_service,
):
    """Handler returns NormalizationResult with unknown slug → no fact created, run completes."""
    subject_id_holder: list[uuid.UUID] = []

    async def _seed_and_run(session_fac):
        async with session_fac() as session:
            app_id = await _make_application(session)
            subject_id = await _make_subject(session)
            subject_id_holder.append(subject_id)
            res_id = await _make_resource(session, app_id)
            await session.flush()

            def _make_result(artifact):
                return [
                    NormalizationResult(
                        subject_id=subject_id,
                        account_id=None,
                        resource_id=res_id,
                        action_slug='nonexistent_slug',
                        effect='allow',
                        valid_from=None,
                        valid_until=None,
                    )
                ]

            register_handler('bad_slug_type', _make_handler(_make_result))
            await _seed_artifact(session, app_id, 'bad_slug_type', {})
            await session.flush()

            summary = await run_reconciliation(
                session,
                application_id=app_id,
                access_fact_service=access_fact_service,
                artifact_binding_service=artifact_binding_service,
            )
            await session.commit()
            return summary

    summary = await _seed_and_run(session_factory)
    assert summary.facts_created == 0
    assert summary.artifacts_unhandled == 0
    assert summary.facts_errored == 1


@pytest.mark.asyncio
async def test_missing_resource_handler_creates_it(
    session_factory,
    access_fact_service,
    artifact_binding_service,
):
    """Handler references new resource_key → ensure_resource_by_identity creates it."""
    _register_role_handler()

    async with session_factory() as session:
        app_id = await _make_application(session)
        subject_id = await _make_subject(session)

        # No pre-existing resource
        payload = {
            'subject_id': str(subject_id),
            'resource_key': 'brand-new-resource',
            'resource_type': 'table',
            'action_slug': 'read',
            'effect': 'allow',
        }
        await _seed_artifact(session, app_id, 'role', payload)
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert summary.facts_created == 1


@pytest.mark.asyncio
async def test_binding_idempotency_on_rerun(
    session_factory,
    access_fact_service,
    artifact_binding_service,
    capturing_events,
):
    """Running twice with identical artifacts → second run: 0 new facts, 0 new bindings."""
    _register_role_handler()

    async with session_factory() as session:
        app_id = await _make_application(session)
        subject_id = await _make_subject(session)

        payload = {
            'subject_id': str(subject_id),
            'resource_key': 'idempotent-resource',
            'resource_type': 'database',
            'action_slug': 'read',
            'effect': 'allow',
        }
        await _seed_artifact(session, app_id, 'role', payload)
        await session.flush()

        # First run
        summary1 = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert summary1.facts_created == 1

    # Reset event capture for second run
    capturing_events.emitted.clear()

    async with session_factory() as session:
        # Second run with same state
        summary2 = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert summary2.facts_created == 0
    assert summary2.facts_revoked == 0
    assert summary2.facts_updated == 0
    new_bindings = capturing_events.filter_by_type('inventory.artifact_binding.created')
    assert len(new_bindings) == 0


@pytest.mark.asyncio
async def test_subject_only_fact_no_account_scope_check(
    session_factory,
    access_fact_service,
    artifact_binding_service,
):
    """NormalizationResult with account_id=None → no scope check, fact created."""
    _register_role_handler()

    async with session_factory() as session:
        app_id = await _make_application(session)
        subject_id = await _make_subject(session)

        payload = {
            'subject_id': str(subject_id),
            'resource_key': 'scope-check-res',
            'resource_type': 'database',
            'action_slug': 'read',
            'effect': 'allow',
        }
        await _seed_artifact(session, app_id, 'role', payload)
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert summary.facts_created == 1


@pytest.mark.asyncio
async def test_set_diff_creates_missing_facts(
    session_factory,
    access_fact_service,
    artifact_binding_service,
):
    """2 artifacts, 1 matching current fact, 1 extra current fact → 1 created, 1 revoked."""
    _register_role_handler()

    async with session_factory() as session:
        app_id = await _make_application(session)
        subject_id = await _make_subject(session)
        action_id = await _resolve_action_id(session, 'read')

        # Pre-existing fact #1 (will be in overlap)
        res1 = await _make_resource(session, app_id)
        await session.flush()
        from src.inventory.resources.repository import get_resource_by_id

        resource1 = await get_resource_by_id(session, res1)
        await _seed_fact(session, subject_id, res1, action_id)

        # Pre-existing fact #2 (will be revoked)
        res2 = await _make_resource(session, app_id)
        await _seed_fact(session, subject_id, res2, action_id)

        # Artifact for resource1 (overlap) and resource3 (new)
        for rkey, rtype in [(resource1.resource_key, resource1.resource_type), ('brand-new-3', 'database')]:
            payload = {
                'subject_id': str(subject_id),
                'resource_key': rkey,
                'resource_type': rtype,
                'action_slug': 'read',
                'effect': 'allow',
            }
            await _seed_artifact(session, app_id, 'role', payload)
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    # res2 revoked, res3 created, res1 stays (no field drift = no update)
    assert summary.facts_revoked == 1
    assert summary.facts_created == 1


@pytest.mark.asyncio
async def test_bulk_revoke_warning_level(
    session_factory,
    access_fact_service,
    artifact_binding_service,
):
    """101 facts to revoke → summary.facts_revoked > 100."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        subject_id = await _make_subject(session)
        action_id = await _resolve_action_id(session, 'read')

        for _ in range(101):
            res_id = await _make_resource(session, app_id)
            await _seed_fact(session, subject_id, res_id, action_id)
        await session.flush()

        summary = await run_reconciliation(
            session,
            application_id=app_id,
            access_fact_service=access_fact_service,
            artifact_binding_service=artifact_binding_service,
        )
        await session.commit()

    assert summary.facts_revoked > 100
