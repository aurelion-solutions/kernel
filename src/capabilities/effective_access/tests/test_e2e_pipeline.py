# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Phase 09 Step 7 — end-to-end pipeline test.

Proves the full seam:
  raw ACL payload
  → Phase 08 normalization (ACLNormalizerService)
  → inventory.access_fact.* / inventory.initiative.* events → CapturingEventService
  → EAS incremental consumer (_handle_message_async)
  → effective_grants rows
  → read API (GET /effective-grants, GET /effective-grants/explain)

No live RabbitMQ. No runtime code changed. No new routes. No migrations.

Design: Option B (hybrid, no broker). The test captures EventEnvelopes emitted by
real inventory services via CapturingEventService, then feeds each relevant envelope
directly into ``_handle_message_async`` — serialized via ``envelope.model_dump(mode='json')``
+ ``json.dumps(...).encode('utf-8')``.

Four waves:
  W1 — ingest 2 ACL rows + create 1 initiative per fact → 2 active grants
  W2 — expire initiative 1 → 1 active grant, 1 tombstoned
  W3 — invalidate fact 2   → 0 active grants, 2 tombstoned
  W4 — CAS replay (replay Wave-1 initiative.created envelopes) → no resurrection
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.capabilities.effective_access.service import EffectiveAccessProjectionService
from src.capabilities.normalization.acl.schemas import ACLEntryPayload
from src.capabilities.normalization.acl.service import ACLNormalizerService
from src.inventory.access_artifacts.service import AccessArtifactService
from src.inventory.access_facts.service import AccessFactService
from src.inventory.artifact_bindings.service import ArtifactBindingService
from src.inventory.customers.models import Customer
from src.inventory.initiatives.models import InitiativeType
from src.inventory.initiatives.schemas import InitiativePatch
from src.inventory.initiatives.service import InitiativeService
from src.inventory.resources.service import ResourceService
from src.inventory.subjects.models import Subject, SubjectKind
from src.platform.applications.models import Application
from src.platform.events.schemas import EventEnvelope
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService
from src.runtimes.mq_eas_projection_consumer.handler import (
    _EVENT_TYPES_RELEVANT,
    _handle_message_async,
)
from src.runtimes.mq_eas_projection_consumer.tests.test_handler import (
    CapturingLogService,
)

# ---------------------------------------------------------------------------
# Fixture payloads — two ACL rows on different resources, same subject
# ---------------------------------------------------------------------------

_ROW_1 = ACLEntryPayload(
    resource_external_id='/repo/core/src',
    resource_kind='folder',
    verb='read',
    effect='allow',
    environment='production',
    data_sensitivity='financial',
)
_ROW_2 = ACLEntryPayload(
    resource_external_id='/repo/public/docs',
    resource_kind='folder',
    verb='read',
    effect='allow',
    environment='production',
    data_sensitivity='public',
)


# ---------------------------------------------------------------------------
# Module-scoped helper — drain relevant envelopes from CapturingEventService
# ---------------------------------------------------------------------------


def _drain_relevant_envelopes(
    capturing: CapturingEventService,
    *,
    already_consumed: int,
) -> tuple[list[EventEnvelope], int]:
    """Return (new_relevant_envelopes, new_cursor).

    Reads all emitted envelopes, returns those at index >= already_consumed
    whose event_type is in _EVENT_TYPES_RELEVANT. The second tuple element
    is the updated cursor (total envelope count) for the next call.
    """
    all_envs = capturing.emitted
    new_envs = all_envs[already_consumed:]
    new_cursor = len(all_envs)
    relevant = [e for e in new_envs if e.event_type in _EVENT_TYPES_RELEVANT]
    return relevant, new_cursor


# ---------------------------------------------------------------------------
# Local prerequisite builder (inlined to avoid cross-slice test coupling)
# ---------------------------------------------------------------------------


async def _make_e2e_prerequisites(session: AsyncSession) -> dict[str, Any]:
    """Create Application, Customer, Subject for the e2e test."""
    app = Application(
        name='eas-e2e-pipeline',
        code=f'eas-e2e-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()

    customer = Customer(external_id=str(uuid.uuid4()))
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


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eas_pipeline_end_to_end(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    tmp_path,
) -> None:
    """Phase 09 e2e: 4-wave pipeline from ACL ingest to read API with CAS guard."""

    # ------------------------------------------------------------------
    # Pre-wave setup
    # ------------------------------------------------------------------
    from pathlib import Path

    log_file: Path = tmp_path / 'logs.jsonl'
    log_factory = LogSinkFactory()
    log_factory.register('file', lambda: FileLogSink(path=log_file))
    inventory_log = LogService(sink=log_factory.get('file'))

    producer_captured_events = CapturingEventService()
    producer_event_service = EventService(sink=producer_captured_events)

    consumer_log = CapturingLogService()
    _capturing_consumer_events = CapturingEventService()
    consumer_events = EventService(sink=_capturing_consumer_events)

    fact_svc = AccessFactService(event_service=producer_event_service)
    acl_svc = ACLNormalizerService(
        artifact_service=AccessArtifactService(event_service=producer_event_service),
        resource_service=ResourceService(event_service=producer_event_service),
        access_fact_service=fact_svc,  # shared instance — reused in Wave 3
        binding_service=ArtifactBindingService(event_service=producer_event_service),
        log_service=inventory_log,
    )
    init_svc = InitiativeService(event_service=producer_event_service)

    async with session_factory() as session:
        ids = await _make_e2e_prerequisites(session)
        app_id: uuid.UUID = ids['application_id']
        subject_id: uuid.UUID = ids['subject_id']
        await session.commit()

    cursor = 0

    # ------------------------------------------------------------------
    # Drive consumer helper — feeds newly captured relevant envelopes to the handler
    # ------------------------------------------------------------------

    async def _drive_consumer(current_cursor: int) -> int:
        new_envs, new_cursor = _drain_relevant_envelopes(producer_captured_events, already_consumed=current_cursor)
        for envelope in new_envs:
            body = json.dumps(envelope.model_dump(mode='json')).encode('utf-8')
            await _handle_message_async(
                body,
                routing_key=envelope.event_type,
                session_factory=session_factory,
                projection_service_factory=lambda s, es: EffectiveAccessProjectionService(s, event_service=es),
                log_service=consumer_log,  # type: ignore[arg-type]
                event_service=consumer_events,
            )
        return new_cursor

    # ==================================================================
    # Wave 1 — ingest 2 ACL rows + create 1 initiative per fact
    # ==================================================================

    async with session_factory() as session:
        n1 = await acl_svc.ingest_and_normalize(
            session,
            application_id=app_id,
            subject_id=subject_id,
            account_id=None,
            payload=_ROW_1,
            artifact_external_id='line-1',
            ingest_batch_id='e2e-w1',
        )
        n2 = await acl_svc.ingest_and_normalize(
            session,
            application_id=app_id,
            subject_id=subject_id,
            account_id=None,
            payload=_ROW_2,
            artifact_external_id='line-2',
            ingest_batch_id='e2e-w1',
        )
        await session.commit()

    fact_id_1: uuid.UUID = n1.access_fact_id
    fact_id_2: uuid.UUID = n2.access_fact_id
    resource_id_1: uuid.UUID = n1.resource_id

    async with session_factory() as session:
        init1 = await init_svc.create_initiative(
            session,
            access_fact_id=fact_id_1,
            type_=InitiativeType.requested,
            origin='INC-1001',
            valid_from=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            valid_until=None,
        )
        init2 = await init_svc.create_initiative(
            session,
            access_fact_id=fact_id_2,
            type_=InitiativeType.birthright,
            origin='birthright',
            valid_from=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            valid_until=None,
        )
        await session.commit()

    init_id_1: uuid.UUID = init1.id
    init_id_2: uuid.UUID = init2.id

    cursor = await _drive_consumer(cursor)

    # --- Wave 1 HTTP assertions ---
    resp = await client.get(f'/api/v0/effective-grants?subject_id={subject_id}')
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2, f'Expected 2 active grants after Wave 1, got {len(items)}'

    by_fact = {item['source_access_fact_id']: item for item in items}
    g1 = by_fact[str(fact_id_1)]
    g2 = by_fact[str(fact_id_2)]

    assert g1['tombstoned_at'] is None
    assert g2['tombstoned_at'] is None
    assert g1['source_initiative_id'] == str(init_id_1)
    assert g2['source_initiative_id'] == str(init_id_2)
    assert g1['initiative_type'] == 'requested'
    assert g1['initiative_origin'] == 'INC-1001'
    assert g2['initiative_type'] == 'birthright'
    assert g2['initiative_origin'] == 'birthright'

    # CAS-propagation: observed_at must equal the initiative.created event timestamp.
    # Compare as datetime objects to avoid Z vs +00:00 string format divergence.
    init_created_dt: dict[str, datetime] = {
        e.payload['initiative_id']: e.occurred_at
        for e in producer_captured_events.emitted
        if e.event_type == 'inventory.initiative.created'
    }
    assert datetime.fromisoformat(g1['observed_at']) == init_created_dt[str(init_id_1)]
    assert datetime.fromisoformat(g2['observed_at']) == init_created_dt[str(init_id_2)]

    # Capture for later invariance checks
    pre_g2_observed_at: str = g2['observed_at']

    # Explain assertion for the first grant
    resp = await client.get(
        '/api/v0/effective-grants/explain',
        params={
            'subject_id': str(subject_id),
            'resource_id': str(resource_id_1),
            'action': 'read',
        },
    )
    assert resp.status_code == 200
    explain_body = resp.json()
    assert explain_body['effect'] == 'allow'
    assert len(explain_body['grants']) == 1
    assert explain_body['grants'][0]['source_initiative_id'] == str(init_id_1)

    # ==================================================================
    # Wave 2 — expire initiative 1 → tombstone its grant
    # ==================================================================

    t_past = datetime(2020, 1, 1, 0, 0, tzinfo=UTC)
    async with session_factory() as session:
        await init_svc.update_initiative(session, init_id_1, InitiativePatch(valid_until=t_past))
        await session.commit()

    cursor = await _drive_consumer(cursor)

    # active_only=true (default) returns only the still-active grant
    resp = await client.get(f'/api/v0/effective-grants?subject_id={subject_id}')
    assert resp.status_code == 200
    active_items = resp.json()
    assert len(active_items) == 1, f'Expected 1 active grant after Wave 2, got {len(active_items)}'
    assert active_items[0]['source_initiative_id'] == str(init_id_2)

    # active_only=false returns both — one tombstoned, one alive
    resp = await client.get(f'/api/v0/effective-grants?subject_id={subject_id}&active_only=false')
    assert resp.status_code == 200
    all_items = resp.json()
    assert len(all_items) == 2
    by_fact = {item['source_access_fact_id']: item for item in all_items}
    assert by_fact[str(fact_id_1)]['tombstoned_at'] is not None
    assert by_fact[str(fact_id_2)]['tombstoned_at'] is None

    # Scope correctness: grant 2 is untouched by Wave 2
    assert by_fact[str(fact_id_2)]['observed_at'] == pre_g2_observed_at

    # Tombstone timestamp == the occurred_at of the first event that tombstoned this grant.
    # With inventory.initiative.updated now in the UPSERT set, the UPSERT path runs
    # tombstone_effective_grants_for_missing_pairs with its observed_at; the subsequent
    # inventory.initiative.expired handler call is a no-op (grant already tombstoned).
    # So tombstoned_at == occurred_at of whichever relevant Wave-2 envelope hit first.
    # We verify that tombstoned_at is non-null and within the Wave-2 envelope timestamps.
    wave2_init1_envs = [
        e
        for e in producer_captured_events.emitted
        if e.payload.get('initiative_id') == str(init_id_1)
        and e.event_type in ('inventory.initiative.updated', 'inventory.initiative.expired')
    ]
    assert len(wave2_init1_envs) >= 1
    wave2_timestamps = {e.occurred_at for e in wave2_init1_envs}
    assert datetime.fromisoformat(by_fact[str(fact_id_1)]['tombstoned_at']) in wave2_timestamps

    # Capture Wave-2 tombstone for the Wave-3 and Wave-4 invariance checks
    prior_fact1_tombstone: str = by_fact[str(fact_id_1)]['tombstoned_at']

    # ==================================================================
    # Wave 3 — invalidate fact 2 → no active grants remain
    # ==================================================================

    async with session_factory() as session:
        from uuid import uuid4 as _uuid4

        revoke_at = datetime.now(UTC)
        await fact_svc.revoke_fact(session, fact_id_2, delta_item_id=_uuid4(), observed_at=revoke_at)
        await session.commit()

    # Step 12: AccessFactService no longer emits inventory.access_fact.revoked events.
    # SyncApplyService would emit them after an Iceberg write. For the EAS e2e test,
    # we manually emit the event to simulate what SyncApplyService would do.
    from src.platform.events.schemas import EventParticipantKind
    from src.platform.events.schemas import new_event_envelope as _new_ev

    revoke_event = _new_ev(
        event_type='inventory.access_fact.revoked',
        occurred_at=revoke_at,
        correlation_id=uuid.uuid4().hex,
        payload={
            'fact_id': str(fact_id_2),
            'delta_item_id': str(uuid.uuid4()),
            'reconciliation_run_id': str(uuid.uuid4()),
            'snapshot_id': None,
            'subject_id': str(subject_id),
            'resource_id': str(uuid.uuid4()),
            'action_id': 1,
            'effect': 'allow',
            'natural_key_hash': 'a' * 64,
            'revoked_at': revoke_at.isoformat(),
        },
        actor_kind=EventParticipantKind.CAPABILITY,
        actor_id='capabilities.sync_apply',
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(fact_id_2),
    )
    await producer_event_service.emit(revoke_event)

    cursor = await _drive_consumer(cursor)

    # active_only=true returns empty
    resp = await client.get(f'/api/v0/effective-grants?subject_id={subject_id}')
    assert resp.status_code == 200
    assert resp.json() == []

    # active_only=false: both tombstoned
    resp = await client.get(f'/api/v0/effective-grants?subject_id={subject_id}&active_only=false')
    assert resp.status_code == 200
    all_items = resp.json()
    assert len(all_items) == 2
    after = {item['source_access_fact_id']: item for item in all_items}
    assert after[str(fact_id_1)]['tombstoned_at'] is not None
    assert after[str(fact_id_2)]['tombstoned_at'] is not None

    # Wave-2 tombstone on fact 1 is not overwritten by the Wave-3 fact-2-scoped invalidate
    assert after[str(fact_id_1)]['tombstoned_at'] == prior_fact1_tombstone

    # CAS-propagation: fact 2 tombstoned_at == inventory.access_fact.revoked event occurred_at.
    # Compare as datetime objects to avoid Z vs +00:00 string format divergence.
    invalidated_env = next(
        e
        for e in producer_captured_events.emitted
        if e.event_type == 'inventory.access_fact.revoked' and e.payload.get('fact_id') == str(fact_id_2)
    )
    assert datetime.fromisoformat(after[str(fact_id_2)]['tombstoned_at']) == invalidated_env.occurred_at

    # ==================================================================
    # Wave 4 — CAS guard: replay Wave-1 initiative.created envelopes
    # ==================================================================

    w1_init_created_envs = [
        e for e in producer_captured_events.emitted if e.event_type == 'inventory.initiative.created'
    ]
    assert len(w1_init_created_envs) == 2, (
        f'Expected exactly 2 inventory.initiative.created envelopes for the replay, got {len(w1_init_created_envs)}'
    )

    for envelope in w1_init_created_envs:
        body = json.dumps(envelope.model_dump(mode='json')).encode('utf-8')
        await _handle_message_async(
            body,
            routing_key=envelope.event_type,
            session_factory=session_factory,
            projection_service_factory=lambda s, es: EffectiveAccessProjectionService(s, event_service=es),
            log_service=consumer_log,  # type: ignore[arg-type]
            event_service=consumer_events,
        )

    # No resurrection — active_only=true still returns empty
    resp = await client.get(f'/api/v0/effective-grants?subject_id={subject_id}')
    assert resp.status_code == 200
    assert resp.json() == []

    # active_only=false: still exactly 2 rows, tombstones unchanged (CAS rejected)
    resp = await client.get(f'/api/v0/effective-grants?subject_id={subject_id}&active_only=false')
    assert resp.status_code == 200
    final_items = resp.json()
    assert len(final_items) == 2
    final = {item['source_access_fact_id']: item for item in final_items}
    assert final[str(fact_id_1)]['tombstoned_at'] == prior_fact1_tombstone
    assert datetime.fromisoformat(final[str(fact_id_2)]['tombstoned_at']) == invalidated_env.occurred_at  # revoked

    # No ERROR events from the consumer during any of the four waves
    error_events = [e for e in consumer_log.events if e[1].value == 'error']
    assert error_events == [], f'Unexpected consumer ERROR events: {error_events}'
