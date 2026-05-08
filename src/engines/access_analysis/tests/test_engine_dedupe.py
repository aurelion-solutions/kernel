# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine deduplication tests — same scan twice → reuse, evidence_hash stability."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
import sqlalchemy as sa
from src.engines.access_analysis.engine import ScanEngine
from src.engines.access_analysis.tests.conftest import (
    seed_application,
    seed_capability,
    seed_capability_grant,
    seed_effective_grant,
    seed_mapping,
    seed_scope_key,
    seed_sod_rule,
    seed_subject,
)
from src.inventory.assessment.findings.models import Finding
from src.inventory.assessment.scan_runs.models import ScanRun, ScanRunTrigger
from src.inventory.policy.sod_rule_conditions.models import (
    SodRuleCondition,
    sod_rule_condition_capabilities,
)
from src.inventory.policy.sod_rules.models import SodSeverity
from src.platform.logs.service import NoOpLogService


async def _setup_sod_violation_fixture(session_factory):
    """Return run_id after seeding a complete SoD violation scenario."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        scope_key_id = await seed_scope_key(session)

        cap_a_id = await seed_capability(session, f'cap_dd_a_{uuid.uuid4().hex[:6]}')
        cap_b_id = await seed_capability(session, f'cap_dd_b_{uuid.uuid4().hex[:6]}')
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

        await session.commit()

    return subject_id


@pytest.mark.asyncio
async def test_second_run_reuses_findings(session_factory, engine_test_lake_session) -> None:
    """Running the same scan twice: second run reuses all findings, no new rows in DB."""
    await _setup_sod_violation_fixture(session_factory)

    # Run 1
    async with session_factory() as session:
        run1 = ScanRun(triggered_by=ScanRunTrigger.manual)
        session.add(run1)
        await session.flush()
        await session.refresh(run1)

        engine = ScanEngine()
        at = datetime.now(UTC)
        result1 = await engine.run(
            session,
            run1,
            at=at,
            correlation_id='dedup-run1',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    assert result1.error is None
    created_count = len(result1.findings_created)
    assert created_count >= 1

    # Run 2 — same data, expect all reused
    async with session_factory() as session:
        run2 = ScanRun(triggered_by=ScanRunTrigger.manual)
        session.add(run2)
        await session.flush()
        await session.refresh(run2)

        engine = ScanEngine()
        at = datetime.now(UTC)
        result2 = await engine.run(
            session,
            run2,
            at=at,
            correlation_id='dedup-run2',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    assert result2.error is None
    # All findings from run1 should be reused in run2
    assert len(result2.findings_reused) == result1.findings_total
    assert len(result2.findings_created) == 0

    # Total finding rows in DB should equal run1's created count
    async with session_factory() as session:
        result = await session.execute(sa.select(sa.func.count()).select_from(Finding))
        total_rows = result.scalar_one()

    assert total_rows == created_count


@pytest.mark.asyncio
async def test_dedup_counts_match_across_runs(session_factory, engine_test_lake_session) -> None:
    """findings_total is consistent across deduplicated runs."""
    await _setup_sod_violation_fixture(session_factory)

    async with session_factory() as session:
        run1 = ScanRun(triggered_by=ScanRunTrigger.manual)
        session.add(run1)
        await session.flush()
        await session.refresh(run1)
        engine = ScanEngine()
        result1 = await engine.run(
            session,
            run1,
            at=datetime.now(UTC),
            correlation_id='c1',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    async with session_factory() as session:
        run2 = ScanRun(triggered_by=ScanRunTrigger.manual)
        session.add(run2)
        await session.flush()
        await session.refresh(run2)
        engine = ScanEngine()
        result2 = await engine.run(
            session,
            run2,
            at=datetime.now(UTC),
            correlation_id='c2',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    # Second run: total = reused (no new creations for sod findings)
    assert result2.findings_total == result1.findings_total
    assert result2.findings_total == len(result2.findings_reused) + len(result2.findings_created)
