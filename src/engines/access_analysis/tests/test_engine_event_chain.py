# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine event chain tests — correlation_id consistency and causation_id chaining."""

from __future__ import annotations

import uuid

import pytest
from src.engines.access_analysis.service import (
    ScanOrchestrationService,
)
from src.engines.access_analysis.tests.conftest import (
    seed_application,
    seed_capability,
    seed_capability_grant,
    seed_effective_grant,
    seed_mapping,
    seed_pending_scan_run,
    seed_scope_key,
    seed_sod_rule,
    seed_subject,
)
from src.inventory.assessment.scan_runs.models import ScanRun
from src.inventory.policy.sod_rule_conditions.models import (
    SodRuleCondition,
    sod_rule_condition_capabilities,
)
from src.inventory.policy.sod_rules.models import SodSeverity
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService


@pytest.mark.asyncio
async def test_event_chain_correlation_id_shared(session_factory, engine_test_lake_session) -> None:
    """All events in a scan share the same correlation_id."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        scope_key_id = await seed_scope_key(session)

        cap_a_id = await seed_capability(session, f'cap_ec_a_{uuid.uuid4().hex[:6]}')
        cap_b_id = await seed_capability(session, f'cap_ec_b_{uuid.uuid4().hex[:6]}')
        rule_id = await seed_sod_rule(session, severity=SodSeverity.high)

        cond_a = SodRuleCondition(rule_id=rule_id, min_count=1)
        cond_b = SodRuleCondition(rule_id=rule_id, min_count=1)
        session.add(cond_a)
        session.add(cond_b)
        await session.flush()
        await session.execute(
            sod_rule_condition_capabilities.insert().values(condition_id=cond_a.id, capability_id=cap_a_id)
        )
        await session.execute(
            sod_rule_condition_capabilities.insert().values(condition_id=cond_b.id, capability_id=cap_b_id)
        )
        await session.flush()

        mapping_a_id = await seed_mapping(session, cap_a_id, app_id, scope_key_id)
        mapping_b_id = await seed_mapping(session, cap_b_id, app_id, scope_key_id)
        eg_a = await seed_effective_grant(session, subject_id, app_id)
        eg_b = await seed_effective_grant(session, subject_id, app_id)
        await seed_capability_grant(session, subject_id, cap_a_id, app_id, scope_key_id, eg_a, mapping_a_id)
        await seed_capability_grant(session, subject_id, cap_b_id, app_id, scope_key_id, eg_b, mapping_b_id)

        run = await seed_pending_scan_run(session)
        await session.commit()

    capturing = CapturingEventService()
    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        orch = ScanOrchestrationService(
            session=session,
            events=capturing,
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
        )
        await orch.run_scan(run.id, correlation_id='fixed-corr')
        await session.commit()

    # All events share the same correlation_id
    assert len(capturing.emitted) >= 3  # started + at least 1 finding.created + completed
    correlation_ids = {e.correlation_id for e in capturing.emitted}
    assert len(correlation_ids) == 1
    assert 'fixed-corr' in correlation_ids


@pytest.mark.asyncio
async def test_event_chain_causation_ids(session_factory, engine_test_lake_session) -> None:
    """finding.created and scan.completed causation_id == scan.started event_id."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        scope_key_id = await seed_scope_key(session)

        cap_a_id = await seed_capability(session, f'cap_ci_a_{uuid.uuid4().hex[:6]}')
        cap_b_id = await seed_capability(session, f'cap_ci_b_{uuid.uuid4().hex[:6]}')
        rule_id = await seed_sod_rule(session, severity=SodSeverity.high)

        cond_a = SodRuleCondition(rule_id=rule_id, min_count=1)
        cond_b = SodRuleCondition(rule_id=rule_id, min_count=1)
        session.add(cond_a)
        session.add(cond_b)
        await session.flush()
        await session.execute(
            sod_rule_condition_capabilities.insert().values(condition_id=cond_a.id, capability_id=cap_a_id)
        )
        await session.execute(
            sod_rule_condition_capabilities.insert().values(condition_id=cond_b.id, capability_id=cap_b_id)
        )
        await session.flush()

        mapping_a_id = await seed_mapping(session, cap_a_id, app_id, scope_key_id)
        mapping_b_id = await seed_mapping(session, cap_b_id, app_id, scope_key_id)
        eg_a = await seed_effective_grant(session, subject_id, app_id)
        eg_b = await seed_effective_grant(session, subject_id, app_id)
        await seed_capability_grant(session, subject_id, cap_a_id, app_id, scope_key_id, eg_a, mapping_a_id)
        await seed_capability_grant(session, subject_id, cap_b_id, app_id, scope_key_id, eg_b, mapping_b_id)

        run = await seed_pending_scan_run(session)
        await session.commit()

    capturing = CapturingEventService()
    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        orch = ScanOrchestrationService(
            session=session,
            events=capturing,
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
        )
        await orch.run_scan(run.id)
        await session.commit()

    started_events = capturing.filter_by_type('access_analysis.scan.started')
    completed_events = capturing.filter_by_type('access_analysis.scan.completed')
    created_events = capturing.filter_by_type('access_analysis.finding.created')

    assert len(started_events) == 1
    assert len(completed_events) == 1
    assert len(created_events) >= 1

    started_event_id = started_events[0].event_id
    # started has no causation
    assert started_events[0].causation_id is None

    # All finding.created and scan.completed share causation_id == started event_id
    for e in created_events:
        assert e.causation_id == started_event_id
    assert completed_events[0].causation_id == started_event_id


@pytest.mark.asyncio
async def test_event_payload_contains_scan_run_id(session_factory, engine_test_lake_session) -> None:
    """scan_run_id must be present in event payloads, never in correlation_id."""
    async with session_factory() as session:
        run = await seed_pending_scan_run(session)
        await session.commit()

    capturing = CapturingEventService()
    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        orch = ScanOrchestrationService(
            session=session,
            events=capturing,
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
        )
        await orch.run_scan(run.id)
        await session.commit()

    run_id = run.id
    for event in capturing.emitted:
        assert 'scan_run_id' in event.payload
        assert event.payload['scan_run_id'] == run_id
        # correlation_id must not be the run id
        assert event.correlation_id != str(run_id)
