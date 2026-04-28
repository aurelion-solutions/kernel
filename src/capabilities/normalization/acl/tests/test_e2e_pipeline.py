# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end pipeline test — Phase 08 capstone.

Runs ingest → normalize → bind for 3 ACL rows × 2 passes (replay),
then asserts row counts, shapes, and event footprint.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from src.capabilities.normalization.acl.schemas import ACLEntryPayload
from src.capabilities.normalization.acl.service import ACLNormalizerService
from src.inventory.access_artifacts.service import AccessArtifactService
from src.inventory.access_facts.schemas import AccessFactEffect
from src.inventory.access_facts.service import AccessFactService
from src.inventory.artifact_bindings.models import ArtifactBinding
from src.inventory.artifact_bindings.service import ArtifactBindingService
from src.inventory.enums import Action
from src.inventory.resources.models import Resource, ResourcePrivilegeLevel
from src.inventory.resources.service import ResourceService
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService

_FIXTURE_ROWS = [
    ACLEntryPayload(
        resource_external_id='/repo/core/src',
        resource_kind='folder',
        verb='read',
        effect='allow',
        environment='production',
        data_sensitivity='financial',
    ),
    ACLEntryPayload(
        resource_external_id='/repo/core/src',
        resource_kind='folder',
        verb='write',
        effect='allow',
        environment='production',
        data_sensitivity='financial',
    ),
    ACLEntryPayload(
        resource_external_id='/repo/public/docs',
        resource_kind='folder',
        verb='read',
        effect='allow',
        environment='production',
        data_sensitivity='public',
    ),
]


async def _make_e2e_prerequisites(session):
    """Create Application, Customer, Subject for the e2e test."""
    import uuid

    from src.inventory.customers.models import Customer
    from src.inventory.subjects.models import Subject, SubjectKind
    from src.platform.applications.models import Application

    app = Application(
        name='filesvc-acl-e2e',
        code=f'filesvc-acl-e2e-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()

    customer = Customer(
        external_id=str(uuid.uuid4()),
    )
    session.add(customer)
    await session.flush()

    subject = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.customer,
        principal_customer_id=customer.id,
        status='active',
    )
    session.add(subject)
    await session.flush()

    return {'application_id': app.id, 'subject_id': subject.id}


@pytest.mark.asyncio
async def test_acl_pipeline_end_to_end(
    session_factory,
    tmp_path,
) -> None:
    """Phase 08 capstone: ingest 3 ACL rows × 2 passes, assert counts, shapes, and events."""
    from pathlib import Path

    log_file: Path = tmp_path / 'logs.jsonl'
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=log_file))
    log_service = LogService(sink=factory.get('file'))

    capturing_events = CapturingEventService()
    event_service = EventService(sink=capturing_events)

    acl_svc = ACLNormalizerService(
        artifact_service=AccessArtifactService(event_service=event_service),
        resource_service=ResourceService(event_service=event_service),
        access_fact_service=AccessFactService(event_service=event_service),
        binding_service=ArtifactBindingService(event_service=event_service),
        log_service=log_service,
    )

    async with session_factory() as session:
        ids = await _make_e2e_prerequisites(session)
        app_id = ids['application_id']
        subject_id = ids['subject_id']

        # Pass 1 — first ingest of each row.
        for i, row in enumerate(_FIXTURE_ROWS):
            await acl_svc.ingest_and_normalize(
                session,
                application_id=app_id,
                subject_id=subject_id,
                account_id=None,
                payload=row,
                artifact_external_id=f'line-{i}-pass-1',
                ingest_batch_id='e2e-batch-1',
            )

        # Pass 2 — replay identical payloads.
        for i, row in enumerate(_FIXTURE_ROWS):
            await acl_svc.ingest_and_normalize(
                session,
                application_id=app_id,
                subject_id=subject_id,
                account_id=None,
                payload=row,
                artifact_external_id=f'line-{i}-pass-2',
                ingest_batch_id='e2e-batch-2',
            )

        await session.commit()

    # --- Row counts (raw SQL — ORM models deleted Phase 15 Step 16) ---
    async with session_factory() as session:
        artifact_count = (
            await session.execute(
                text('SELECT COUNT(*) FROM access_artifacts WHERE application_id = :app_id'),
                {'app_id': app_id},
            )
        ).scalar_one()
        resource_count = (
            await session.execute(select(func.count()).select_from(Resource).where(Resource.application_id == app_id))
        ).scalar_one()
        fact_count = (await session.execute(text('SELECT COUNT(*) FROM access_facts'))).scalar_one()
        binding_count = (await session.execute(select(func.count()).select_from(ArtifactBinding))).scalar_one()

    assert artifact_count == 6, f'Expected 6 artifacts, got {artifact_count}'
    assert resource_count == 2, f'Expected 2 resources, got {resource_count}'
    assert fact_count >= 3, f'Expected at least 3 facts, got {fact_count}'
    assert binding_count >= 6, f'Expected at least 6 bindings, got {binding_count}'

    # --- Shape assertions (raw SQL) ---
    async with session_factory() as session:
        facts_result = await session.execute(
            text('SELECT effect FROM access_facts WHERE subject_id = :sid'),
            {'sid': subject_id},
        )
        fact_effects = [r.effect for r in facts_result.all()]

    assert all(e == AccessFactEffect.allow.value for e in fact_effects)

    # Find the read-fact and write-fact for /repo/core/src.
    async with session_factory() as session:
        resources = (await session.execute(select(Resource).where(Resource.application_id == app_id))).scalars().all()

    core_resource = next(r for r in resources if r.external_id == '/repo/core/src')
    assert core_resource.privilege_level in (
        ResourcePrivilegeLevel.read,
        ResourcePrivilegeLevel.write,
    ), 'privilege_level should be one of the two ingested values'

    from src.inventory.actions.models import Action as RefAction

    async with session_factory() as session:
        read_action_id_result = await session.execute(select(RefAction.id).where(RefAction.slug == Action.read.value))
        read_action_id = read_action_id_result.scalar_one()
        write_action_id_result = await session.execute(select(RefAction.id).where(RefAction.slug == Action.write.value))
        write_action_id = write_action_id_result.scalar_one()

        read_fact_row = (
            await session.execute(
                text('SELECT id FROM access_facts WHERE subject_id = :sid AND action_id = :aid LIMIT 1'),
                {'sid': subject_id, 'aid': read_action_id},
            )
        ).one_or_none()
        write_fact_row = (
            await session.execute(
                text('SELECT id FROM access_facts WHERE subject_id = :sid AND action_id = :aid LIMIT 1'),
                {'sid': subject_id, 'aid': write_action_id},
            )
        ).one_or_none()

    assert read_fact_row is not None
    assert write_fact_row is not None

    # --- Event assertions ---
    event_types = [e.event_type for e in capturing_events.emitted]

    assert event_types.count('inventory.access_artifact.ingested') == 6
    assert event_types.count('inventory.resource.created') == 2
    # Step 12: inventory.access_fact.* events are no longer emitted by AccessFactService.
    # They are emitted exclusively from SyncApplyService after Iceberg writes.
    assert event_types.count('inventory.access_fact.created') == 0
    assert event_types.count('inventory.artifact_binding.created') == 6
    assert event_types.count('inventory.access_fact.revoked') == 0

    # No events from the normalization orchestrator itself.
    normalization_event_type_events = sum(1 for et in event_types if et.startswith('normalization.'))
    assert normalization_event_type_events == 0, (
        f'Found {normalization_event_type_events} normalization.* events — should be 0'
    )
