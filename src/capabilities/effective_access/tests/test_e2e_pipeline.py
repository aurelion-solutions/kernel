# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Phase 09 Step 7 — end-to-end pipeline test.

Proves the full seam:
  raw ACL payload
  → Phase 08 normalization (ACLNormalizerService)
  → access_fact.* / initiative.* events → FileLogSink
  → EAS incremental consumer (_handle_message_async)
  → effective_grants rows
  → read API (GET /effective-grants, GET /effective-grants/explain)

No live RabbitMQ. No runtime code changed. No new routes. No migrations.

Design: Option B (hybrid, no broker).  The test tails a JSONL log file written
by real inventory services and feeds each relevant line directly into
``_handle_message_async`` as UTF-8 bytes — byte-for-byte identical to what
``FileLogSink`` wrote on disk (confirmed in TASK §6.1.1: one JSON object per
line, no outer envelope, model_dump(mode='json') compatible with the consumer's
normalize_mq_log_event_payload decoder).

``CapturingLogService`` is imported from the sibling test module; both live
under src/ and share the same testpaths, so the import is legal.

Four waves:
  W1 — ingest 2 ACL rows + create 1 initiative per fact → 2 active grants
  W2 — expire initiative 1 → 1 active grant, 1 tombstoned
  W3 — invalidate fact 2   → 0 active grants, 2 tombstoned
  W4 — CAS replay (replay Wave-1 initiative.created bodies) → no resurrection
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
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
# Module-scoped helper — tail the JSONL log and filter to relevant events
# ---------------------------------------------------------------------------


def _tail_relevant_events(
    log_file: Path,
    *,
    already_consumed: int,
) -> tuple[list[str], int]:
    """Return (new_relevant_event_lines, new_cursor).

    Reads the full file, splits on newlines, parses each non-empty line as
    JSON **only to inspect event_type**, and returns the **raw lines** at
    index >= already_consumed whose event_type is in _EVENT_TYPES_RELEVANT.
    The second tuple element is the updated cursor (total line count), to be
    passed back as already_consumed on the next call.

    Returning raw lines (not parsed dicts) lets the caller feed
    ``line.encode('utf-8')`` straight into ``_handle_message_async`` — the
    exact bytes ``FileLogSink`` wrote, no re-dump round-trip.
    """
    text = log_file.read_text(encoding='utf-8') if log_file.exists() else ''
    all_lines = [ln for ln in text.splitlines() if ln.strip()]
    new_lines = all_lines[already_consumed:]
    new_cursor = len(all_lines)
    relevant: list[str] = []
    for line in new_lines:
        try:
            rec: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get('event_type') in _EVENT_TYPES_RELEVANT:
            relevant.append(line)
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
    tmp_path: Path,
) -> None:
    """Phase 09 e2e: 4-wave pipeline from ACL ingest to read API with CAS guard."""

    # ------------------------------------------------------------------
    # Pre-wave setup
    # ------------------------------------------------------------------
    log_file = tmp_path / 'logs.jsonl'

    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=log_file))
    inventory_log = LogService(factory=factory, provider_name='file')

    consumer_log = CapturingLogService()

    fact_svc = AccessFactService(log_service=inventory_log)
    acl_svc = ACLNormalizerService(
        artifact_service=AccessArtifactService(log_service=inventory_log),
        resource_service=ResourceService(log_service=inventory_log),
        access_fact_service=fact_svc,  # shared instance — reused in Wave 3
        binding_service=ArtifactBindingService(log_service=inventory_log),
        log_service=inventory_log,
    )
    init_svc = InitiativeService(log_service=inventory_log)

    async with session_factory() as session:
        ids = await _make_e2e_prerequisites(session)
        app_id: uuid.UUID = ids['application_id']
        subject_id: uuid.UUID = ids['subject_id']
        await session.commit()

    cursor = 0

    # ------------------------------------------------------------------
    # Drive consumer helper — feeds newly appended relevant lines to the handler
    # ------------------------------------------------------------------

    async def _drive_consumer(current_cursor: int) -> int:
        new_lines, new_cursor = _tail_relevant_events(log_file, already_consumed=current_cursor)
        for line in new_lines:
            body = line.encode('utf-8')
            await _handle_message_async(
                body,
                session_factory=session_factory,
                projection_service_factory=lambda s, ls: EffectiveAccessProjectionService(s, ls),
                log_service=consumer_log,  # type: ignore[arg-type]
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

    # CAS-propagation: observed_at must equal the initiative.created event timestamp
    all_records = [json.loads(ln) for ln in log_file.read_text().splitlines() if ln.strip()]
    init_created_ts: dict[str, str] = {
        r['payload']['initiative_id']: r['timestamp'] for r in all_records if r['event_type'] == 'initiative.created'
    }
    assert g1['observed_at'] == init_created_ts[str(init_id_1)]
    assert g2['observed_at'] == init_created_ts[str(init_id_2)]

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

    # Tombstone timestamp == initiative.expired event timestamp (capture-then-compare)
    all_records = [json.loads(ln) for ln in log_file.read_text().splitlines() if ln.strip()]
    expired_ts = next(
        r['timestamp']
        for r in all_records
        if r['event_type'] == 'initiative.expired' and r['payload']['initiative_id'] == str(init_id_1)
    )
    assert by_fact[str(fact_id_1)]['tombstoned_at'] == expired_ts

    # Capture Wave-2 tombstone for the Wave-3 and Wave-4 invariance checks
    prior_fact1_tombstone: str = by_fact[str(fact_id_1)]['tombstoned_at']

    # ==================================================================
    # Wave 3 — invalidate fact 2 → no active grants remain
    # ==================================================================

    async with session_factory() as session:
        await fact_svc.invalidate_fact(session, fact_id_2)
        await session.commit()

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

    # CAS-propagation: fact 2 tombstoned_at == access_fact.invalidated event timestamp
    all_records = [json.loads(ln) for ln in log_file.read_text().splitlines() if ln.strip()]
    invalidated_ts = next(
        r['timestamp']
        for r in all_records
        if r['event_type'] == 'access_fact.invalidated' and r['payload']['access_fact_id'] == str(fact_id_2)
    )
    assert after[str(fact_id_2)]['tombstoned_at'] == invalidated_ts

    # ==================================================================
    # Wave 4 — CAS guard: replay Wave-1 initiative.created lines
    # ==================================================================

    all_lines = [ln for ln in log_file.read_text().splitlines() if ln.strip()]
    w1_init_created = [line for line in all_lines if json.loads(line)['event_type'] == 'initiative.created']
    assert len(w1_init_created) == 2, (
        f'Expected exactly 2 initiative.created lines for the replay, got {len(w1_init_created)}'
    )

    for line in w1_init_created:
        body = line.encode('utf-8')
        await _handle_message_async(
            body,
            session_factory=session_factory,
            projection_service_factory=lambda s, ls: EffectiveAccessProjectionService(s, ls),
            log_service=consumer_log,  # type: ignore[arg-type]
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
    assert final[str(fact_id_2)]['tombstoned_at'] == invalidated_ts

    # No ERROR events from the consumer during any of the four waves
    error_events = [e for e in consumer_log.events if e[1].value == 'error']
    assert error_events == [], f'Unexpected consumer ERROR events: {error_events}'
