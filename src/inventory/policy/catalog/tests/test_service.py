# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for PolicyCatalogService.

Seed SoD rules and findings via raw SQL; provide a temp cartridge dir
with one or two YAML files; assert the unified projection (incl. counts
and findings_filter).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest_asyncio
import sqlalchemy as sa
from src.inventory.policy.catalog.service import PolicyCatalogService
from src.inventory.policy.enums import (
    AssessmentStrategy,
    DefinitionSource,
    PolicyStatus,
    PolicyType,
)


@pytest_asyncio.fixture
async def pg_session(session_factory: Any) -> Any:  # noqa: ANN401
    async with session_factory() as session:
        yield session
        await session.rollback()
        await session.execute(sa.text('DELETE FROM findings'))
        await session.execute(sa.text('DELETE FROM scan_runs'))
        await session.execute(sa.text('DELETE FROM subjects'))
        await session.execute(sa.text('DELETE FROM nhis'))
        await session.execute(sa.text('DELETE FROM sod_rules'))
        await session.commit()


async def _seed_subject(session: Any) -> str:
    """Insert NHI + Subject (CHECK ck_subjects_principal_exactly_one).
    Returns subject UUID as string.
    """
    import uuid as _uuid

    nhi_id = str(_uuid.uuid4())
    sid = str(_uuid.uuid4())
    await session.execute(
        sa.text(
            """
            INSERT INTO nhis (id, external_id, name, kind)
            VALUES (:id, :ext, :name, 'service_account')
            """
        ),
        {'id': nhi_id, 'ext': f'nhi-{nhi_id[:8]}', 'name': f'test-nhi-{nhi_id[:8]}'},
    )
    await session.execute(
        sa.text(
            """
            INSERT INTO subjects (id, external_id, kind, nhi_kind, principal_nhi_id, status)
            VALUES (:id, :ext, 'nhi', 'service_account', :nhi_id, 'active')
            """
        ),
        {'id': sid, 'ext': f'subj-{sid[:8]}', 'nhi_id': nhi_id},
    )
    await session.commit()
    return sid


async def _seed_sod_rule(session: Any, *, code: str, name: str, is_enabled: bool) -> int:
    row = await session.execute(
        sa.text(
            """
            INSERT INTO sod_rules (code, name, severity, scope_mode, is_enabled, mitigation_allowed)
            VALUES (:code, :name, 'high', 'global', :is_enabled, true)
            RETURNING id
            """
        ),
        {'code': code, 'name': name, 'is_enabled': is_enabled},
    )
    rid = int(row.scalar_one())
    await session.commit()
    return rid


async def _seed_scan_run(session: Any) -> int:
    row = await session.execute(sa.text("INSERT INTO scan_runs (triggered_by) VALUES ('manual') RETURNING id"))
    rid = int(row.scalar_one())
    await session.commit()
    return rid


async def _seed_finding(
    session: Any,
    *,
    scan_run_id: int,
    subject_id: str,
    kind: str,
    severity: str = 'high',
    rule_id: int | None = None,
    seq: int = 0,
) -> None:
    """Seed one open Finding. CHECK ck_findings_subject_or_account requires
    at least one of subject_id / account_id; subject_id is FK to subjects.
    """
    await session.execute(
        sa.text(
            """
            INSERT INTO findings (
                scan_run_id, kind, severity, status, rule_id,
                subject_id, evidence_hash, detected_at, evaluated_at
            )
            VALUES (
                :scan_run_id, :kind, :severity, 'open', :rule_id,
                :subject_id, :hash, NOW(), NOW()
            )
            """
        ),
        {
            'scan_run_id': scan_run_id,
            'kind': kind,
            'severity': severity,
            'rule_id': rule_id,
            'subject_id': subject_id,
            'hash': f'h-{scan_run_id}-{kind}-{rule_id}-{seq}',
        },
    )
    await session.commit()


def _write_cartridge(dir_path: Path, file_name: str, contents: str) -> None:
    (dir_path / file_name).write_text(contents, encoding='utf-8')


_ORPHAN_CARTRIDGE = """\
id: lens.access_risk.orphaned_access
version: 1
name: Orphaned Access
description: An orphaned-access cartridge.
policy_type: access_risk
rule_id: lens.access_risk.orphaned_access
assessment_strategy: deterministic
condition: {}
decision: {}
finding: {}
"""

_UNUSED_CARTRIDGE = """\
id: lens.access_risk.unused_access
version: 2
name: Unused Access
description: An unused-access cartridge.
policy_type: access_risk
rule_id: lens.access_risk.unused_access
assessment_strategy: deterministic
condition: {}
decision: {}
finding: {}
"""


async def test_catalog_unifies_sod_rules_and_cartridges(pg_session: Any, tmp_path: Path) -> None:
    sod_active_id = await _seed_sod_rule(pg_session, code='SOD-001', name='Cashier vs Approver', is_enabled=True)
    await _seed_sod_rule(pg_session, code='SOD-002', name='Disabled Rule', is_enabled=False)

    scan_id = await _seed_scan_run(pg_session)
    sub = await _seed_subject(pg_session)
    # Use 'unused_access' to count cartridge kind — 'orphan_access' has a
    # CHECK constraint forbidding subject_id, which would force us to seed
    # accounts/applications too. unused_access accepts subject-only rows.
    await _seed_finding(pg_session, scan_run_id=scan_id, subject_id=sub, kind='sod', rule_id=sod_active_id, seq=1)
    await _seed_finding(pg_session, scan_run_id=scan_id, subject_id=sub, kind='sod', rule_id=sod_active_id, seq=2)
    await _seed_finding(pg_session, scan_run_id=scan_id, subject_id=sub, kind='unused_access', seq=1)
    await _seed_finding(pg_session, scan_run_id=scan_id, subject_id=sub, kind='unused_access', seq=2)
    await _seed_finding(pg_session, scan_run_id=scan_id, subject_id=sub, kind='unused_access', seq=3)

    cartridges_dir = tmp_path / 'lens'
    cartridges_dir.mkdir()
    _write_cartridge(cartridges_dir, 'orphan.yaml', _ORPHAN_CARTRIDGE)
    _write_cartridge(cartridges_dir, 'unused.yaml', _UNUSED_CARTRIDGE)

    service = PolicyCatalogService(cartridge_root=cartridges_dir)
    result = await service.get_catalog(pg_session)

    items_by_id = {i.id: i for i in result.items}
    assert set(items_by_id) == {
        'sod.rule.SOD-001',
        'sod.rule.SOD-002',
        'lens.access_risk.orphaned_access',
        'lens.access_risk.unused_access',
    }

    sod_active = items_by_id['sod.rule.SOD-001']
    assert sod_active.policy_type is PolicyType.SOD
    assert sod_active.definition_source is DefinitionSource.DB
    assert sod_active.assessment_strategy is AssessmentStrategy.DETERMINISTIC
    assert sod_active.status is PolicyStatus.ACTIVE
    assert sod_active.version is None
    assert sod_active.open_findings_count == 2
    assert sod_active.findings_filter is not None
    assert sod_active.findings_filter.kind is None
    assert sod_active.findings_filter.rule_id == sod_active_id

    sod_disabled = items_by_id['sod.rule.SOD-002']
    assert sod_disabled.status is PolicyStatus.INACTIVE
    assert sod_disabled.open_findings_count == 0
    assert sod_disabled.findings_filter is not None
    assert sod_disabled.findings_filter.rule_id is not None

    orphan = items_by_id['lens.access_risk.orphaned_access']
    assert orphan.policy_type is PolicyType.ACCESS_RISK
    assert orphan.definition_source is DefinitionSource.FILE
    assert orphan.status is PolicyStatus.AVAILABLE
    assert orphan.version == 1
    assert orphan.open_findings_count == 0
    assert orphan.findings_filter is not None
    assert orphan.findings_filter.kind is not None
    assert orphan.findings_filter.kind.value == 'orphan_access'
    assert orphan.findings_filter.rule_id is None

    unused = items_by_id['lens.access_risk.unused_access']
    assert unused.open_findings_count == 3
    assert unused.findings_filter is not None
    assert unused.findings_filter.kind is not None
    assert unused.findings_filter.kind.value == 'unused_access'


async def test_catalog_handles_missing_cartridge_dir(pg_session: Any, tmp_path: Path) -> None:
    """Missing cartridge directory yields empty cartridge contribution, not an error."""
    await _seed_sod_rule(pg_session, code='SOD-009', name='Lonely Rule', is_enabled=True)

    missing = tmp_path / 'does_not_exist'
    service = PolicyCatalogService(cartridge_root=missing)
    result = await service.get_catalog(pg_session)

    ids = {i.id for i in result.items}
    assert ids == {'sod.rule.SOD-009'}
    only = result.items[0]
    assert only.open_findings_count == 0
    assert only.findings_filter is not None
