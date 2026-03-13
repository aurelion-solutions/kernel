# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for ArtifactBinding model constraints and indexes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.artifact_bindings.models import ArtifactBinding

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_employee_subject(session) -> uuid.UUID:
    """Create a minimal employee + subject, return subject.id."""
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


async def _make_application(session) -> uuid.UUID:
    """Create a minimal application, return application.id."""
    from src.platform.applications.models import Application

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


async def _make_resource(session) -> uuid.UUID:
    """Create a minimal application + resource, return resource.id."""
    from src.inventory.resources.models import Resource

    app_id = await _make_application(session)
    resource = Resource(
        external_id=str(uuid.uuid4()),
        application_id=app_id,
        kind='database',
    )
    session.add(resource)
    await session.flush()
    return resource.id


async def _make_account(session, application_id: uuid.UUID) -> uuid.UUID:
    """Create an account for the given application, return account.id."""
    from src.inventory.accounts.models import Account, AccountStatus

    account = Account(
        application_id=application_id,
        username=f'user-{uuid.uuid4().hex[:8]}',
        status=AccountStatus.active,
        meta={},
    )
    session.add(account)
    await session.flush()
    return account.id


async def _make_access_artifact(session, application_id: uuid.UUID) -> uuid.UUID:
    """Create an access artifact, return artifact.id."""
    from src.inventory.access_artifacts.models import AccessArtifact

    artifact = AccessArtifact(
        application_id=application_id,
        source_kind='acl_entry',
        external_id=str(uuid.uuid4()),
        payload={'raw': 'data'},
    )
    session.add(artifact)
    await session.flush()
    return artifact.id


async def _make_access_fact(
    session,
    subject_id: uuid.UUID,
    resource_id: uuid.UUID,
) -> uuid.UUID:
    """Create an access fact, return fact.id."""
    from src.inventory.access_facts.models import AccessFact, AccessFactEffect
    from src.inventory.enums import Action

    fact = AccessFact(
        subject_id=subject_id,
        resource_id=resource_id,
        action=Action.read,
        effect=AccessFactEffect.allow,
    )
    session.add(fact)
    await session.flush()
    return fact.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_binding_creation_stores_all_fields(session_factory) -> None:
    """Happy path: create binding with all three target FKs, verify all fields persisted."""
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id = await _make_application(session)
        resource_id = await _make_resource(session)
        account_id = await _make_account(session, app_id)
        artifact_id = await _make_access_artifact(session, app_id)
        fact_id = await _make_access_fact(session, subject_id, resource_id)

        binding = ArtifactBinding(
            artifact_id=artifact_id,
            access_fact_id=fact_id,
            resource_id=resource_id,
            account_id=account_id,
        )
        session.add(binding)
        await session.flush()
        await session.refresh(binding)

        assert binding.id is not None
        assert binding.artifact_id == artifact_id
        assert binding.access_fact_id == fact_id
        assert binding.resource_id == resource_id
        assert binding.account_id == account_id
        assert binding.created_at is not None


@pytest.mark.asyncio
async def test_artifact_binding_fk_to_artifact(session_factory) -> None:
    """ArtifactBinding with non-existent artifact_id raises IntegrityError."""
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        resource_id = await _make_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)

        binding = ArtifactBinding(
            artifact_id=uuid.uuid4(),  # non-existent
            access_fact_id=fact_id,
        )
        session.add(binding)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_artifact_binding_check_constraint_no_target(session_factory) -> None:
    """ArtifactBinding with all three target FKs NULL raises IntegrityError (CHECK constraint)."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_access_artifact(session, app_id)

        binding = ArtifactBinding(
            artifact_id=artifact_id,
            access_fact_id=None,
            resource_id=None,
            account_id=None,
        )
        session.add(binding)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_artifact_binding_minimal_with_only_access_fact_id(session_factory) -> None:
    """ArtifactBinding with only access_fact_id set succeeds."""
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id = await _make_application(session)
        resource_id = await _make_resource(session)
        artifact_id = await _make_access_artifact(session, app_id)
        fact_id = await _make_access_fact(session, subject_id, resource_id)

        binding = ArtifactBinding(
            artifact_id=artifact_id,
            access_fact_id=fact_id,
            resource_id=None,
            account_id=None,
        )
        session.add(binding)
        await session.flush()
        await session.refresh(binding)

        assert binding.id is not None
        assert binding.access_fact_id == fact_id
        assert binding.resource_id is None
        assert binding.account_id is None
