#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1
# ruff: noqa: E402
"""
Phase 19 diff-states seed — demonstrates two directions of pending state.

Run from aurelion-kernel/:
    uv run python scripts/seed_phase19_diff_states.py

Direction 1 — Reconciled, sync not yet applied
-----------------------------------------------
  ReconciliationRun (GHE)     — 6 delta items (create/update/revoke/reactivate/noop×2),
                                  status=pending_apply, NO SyncApplyRun
  ReconciliationRun (GWS)     — 5 delta items (create/update/revoke/noop, employee entity),
                                  status=pending_apply, NO SyncApplyRun
  ReconciliationRun (partial) — 4 delta items: 2 applied + 1 failed + 1 pending,
                                  SyncApplyRun exists (status=partially_applied)

Direction 2 — Planned, apply not yet started
---------------------------------------------
  AccessPlan (senior_engineer phoenix) — 4 items all proposed, NO access_apply_active row
  AccessPlan (NHI key rotation)        — 3 items all proposed, NO access_apply_active row
  AccessPlan (on_leave revoke, req_confirm=True) — 3 items all proposed
  AccessPlan (partially applied)       — 4 items: 2 done + 1 executing + 1 failed,
                                          access_apply_active row present (hung pipeline)

Idempotent: checked by description/content_hash unique keys. Safe to re-run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib

from dotenv import load_dotenv

load_dotenv()
from src.platform.secrets.factory import register_default_providers

register_default_providers()
from src.core.config import get_settings

settings = get_settings()

import src.engines
import src.inventory
import src.platform

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _pkg in (src.inventory, src.engines, src.platform):
    for _root in map(Path, _pkg.__path__):
        for _p in _root.rglob('models.py'):
            importlib.import_module('.'.join(_p.relative_to(_PROJECT_ROOT).with_suffix('').parts))

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from src.engines.access_plan.models import (
    AccessApplyActive,
    AccessPlan,
    AccessPlanStatus,
    PlanDependency,
    PlanItem,
    PlanItemExecution,
    PlanItemExecutionStatus,
    PlanItemFailureReason,
    PlanItemKind,
)
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from src.engines.inventory_sync.models import (
    SyncApplyResult,
    SyncApplyResultStatus,
    SyncApplyRun,
    SyncApplyRunMode,
    SyncApplyRunStatus,
)
from src.inventory.accounts.models import Account
from src.inventory.resources.models import Resource
from src.inventory.subjects.models import Subject
from src.platform.applications.models import Application

_NOW = datetime(2026, 5, 12, 10, 30, 0, tzinfo=UTC)
_API_BASE = 'http://localhost:8000/api/v0'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skip(label: str) -> None:
    print(f'  SKIP (exists): {label}')


def _created(label: str) -> None:
    print(f'  CREATED: {label}')


def _content_hash(*parts: str) -> str:
    return sha256('|'.join(parts).encode()).hexdigest()[:64]


# ---------------------------------------------------------------------------
# Load references from existing seed
# ---------------------------------------------------------------------------


async def load_references(
    session: AsyncSession,
) -> tuple[
    Application,
    Application,
    dict[str, Subject],
    dict[str, Account],
    dict[str, Resource],
]:
    """Load GHE, GWS, subjects, accounts, resources seeded by seed_phase19_demo.py."""

    r = await session.execute(sa.select(Application).where(Application.code == 'GHE'))
    ghe = r.scalar_one()

    r = await session.execute(sa.select(Application).where(Application.code == 'GWORKSPACE'))
    gws = r.scalar_one()

    subject_ext_map = {
        'senior_engineer': 'subj-emp-se-001',
        'engineer': 'subj-emp-eng-001',
        'on_leave': 'subj-emp-lv-001',
        'manager': 'subj-emp-mgr-001',
        'nhi_ci': 'subj-nhi-ci-001',
    }
    subjects: dict[str, Subject] = {}
    for key, ext in subject_ext_map.items():
        r = await session.execute(sa.select(Subject).where(Subject.external_id == ext))
        subj = r.scalar_one()
        subjects[key] = subj

    # Accounts
    r = await session.execute(
        sa.select(Account).where(
            Account.application_id == ghe.id,
            Account.username == 'alexei.voronov',
        )
    )
    se_ghe = r.scalar_one()

    r = await session.execute(
        sa.select(Account).where(
            Account.application_id == gws.id,
            Account.username == 'alexei.voronov@company.com',
        )
    )
    se_gws = r.scalar_one()

    r = await session.execute(
        sa.select(Account).where(
            Account.application_id == ghe.id,
            Account.username == 'aurelion-ci-sa[bot]',
        )
    )
    nhi_ci_ghe = r.scalar_one()

    r = await session.execute(
        sa.select(Account).where(
            Account.application_id == ghe.id,
            Account.username == 'pavel.morozov',
        )
    )
    lv_ghe = r.scalar_one()

    r = await session.execute(
        sa.select(Account).where(
            Account.application_id == ghe.id,
            Account.username == 'maria.sokolova',
        )
    )
    eng_ghe = r.scalar_one()

    accounts: dict[str, Account] = {
        'se_ghe': se_ghe,
        'se_gws': se_gws,
        'nhi_ci_ghe': nhi_ci_ghe,
        'lv_ghe': lv_ghe,
        'eng_ghe': eng_ghe,
    }

    # Resources
    resource_ext_map = {
        'repo_kernel': 'ghe/repos/aurelion-kernel',
        'repo_gui': 'ghe/repos/aurelion-gui',
        'repo_infra': 'ghe/repos/infrastructure',
        'team_platform': 'ghe/orgs/aurelion/teams/platform',
        'group_platform_parent': 'gws/groups/platform-eng',
        'group_platform_child': 'gws/groups/platform-infra',
        'project_apollo': 'gws/projects/apollo',
    }
    resources: dict[str, Resource] = {}
    for key, ext in resource_ext_map.items():
        r = await session.execute(sa.select(Resource).where(Resource.external_id == ext))
        res = r.scalar_one()
        resources[key] = res

    return ghe, gws, subjects, accounts, resources


# ---------------------------------------------------------------------------
# Direction 1a: GHE reconciliation run — pending_apply, NO sync run
# ---------------------------------------------------------------------------


async def seed_reconcile_ghe_pending(
    session: AsyncSession,
    ghe: Application,
    subjects: dict[str, Subject],
    accounts: dict[str, Account],
    resources: dict[str, Resource],
) -> ReconciliationRun:
    """GHE reconcile run: 4 pending + 2 noop delta items, no SyncApplyRun."""

    run_key = 'seed-diff-ghe-pending-v1'

    # Idempotency: check existing run with this error field used as a stable tag
    r = await session.execute(
        sa.select(ReconciliationRun).where(
            ReconciliationRun.application_id == ghe.id,
            ReconciliationRun.status == ReconciliationRunStatus.pending_apply,
            ReconciliationRun.error == run_key,
        )
    )
    existing = r.scalar_one_or_none()
    if existing is not None:
        _skip(f'ReconciliationRun GHE pending ({run_key})')
        return existing

    run = ReconciliationRun(
        id=uuid.uuid4(),
        application_id=ghe.id,
        entity_type=ReconciliationEntityType.access_fact,
        status=ReconciliationRunStatus.pending_apply,
        started_at=_NOW - timedelta(minutes=5),
        finished_at=_NOW - timedelta(minutes=4),
        created_count=2,
        updated_count=1,
        revoked_count=1,
        unchanged_count=2,
        # Reuse error column as a stable idempotency tag (field is optional, NULL in prod)
        error=run_key,
    )
    session.add(run)
    await session.flush()

    # 1. create — target has a new grant that kernel doesn't know about yet
    delta_create = ReconciliationDeltaItem(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.access_fact,
        operation=ReconciliationDeltaOperation.create,
        subject_id=subjects['senior_engineer'].id,
        account_id=accounts['se_ghe'].id,
        resource_id=resources['repo_infra'].id,
        action_id=1,  # read
        effect='allow',
        natural_key_hash=_content_hash('se_ghe', str(resources['repo_infra'].id), 'read'),
        source_artifact_id=uuid.uuid4(),
        after_json={
            'subject_id': str(subjects['senior_engineer'].id),
            'resource_key': 'ghe/repos/infrastructure',
            'permission': 'read',
            'observed_at': _NOW.isoformat(),
        },
        status=ReconciliationDeltaItemStatus.pending,
    )

    # 2. update — target changed permission write→admin, kernel has old version
    delta_update = ReconciliationDeltaItem(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.access_fact,
        operation=ReconciliationDeltaOperation.update,
        subject_id=subjects['senior_engineer'].id,
        account_id=accounts['se_ghe'].id,
        resource_id=resources['repo_kernel'].id,
        action_id=2,  # write
        effect='allow',
        natural_key_hash=_content_hash('se_ghe', str(resources['repo_kernel'].id), 'write'),
        source_artifact_id=uuid.uuid4(),
        existing_fact_id=uuid.uuid4(),
        before_json={
            'permission': 'write',
            'observed_at': (_NOW - timedelta(days=10)).isoformat(),
        },
        after_json={
            'permission': 'admin',
            'observed_at': _NOW.isoformat(),
        },
        status=ReconciliationDeltaItemStatus.pending,
    )

    # 3. revoke — target removed grant, kernel still holds it as active
    delta_revoke = ReconciliationDeltaItem(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.access_fact,
        operation=ReconciliationDeltaOperation.revoke,
        subject_id=subjects['engineer'].id,
        account_id=accounts['eng_ghe'].id,
        resource_id=resources['team_platform'].id,
        action_id=1,  # read
        effect='allow',
        natural_key_hash=_content_hash('eng_ghe', str(resources['team_platform'].id), 'read'),
        existing_fact_id=uuid.uuid4(),
        before_json={
            'team': 'platform',
            'role': 'member',
            'is_active': True,
        },
        after_json=None,
        status=ReconciliationDeltaItemStatus.pending,
    )

    # 4. reactivate — target re-added a previously revoked grant
    delta_reactivate = ReconciliationDeltaItem(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.access_fact,
        operation=ReconciliationDeltaOperation.reactivate,
        subject_id=subjects['on_leave'].id,
        account_id=accounts['lv_ghe'].id,
        resource_id=resources['repo_gui'].id,
        action_id=1,  # read
        effect='allow',
        natural_key_hash=_content_hash('lv_ghe', str(resources['repo_gui'].id), 'read'),
        source_artifact_id=uuid.uuid4(),
        existing_fact_id=uuid.uuid4(),
        before_json={'is_active': False, 'revoked_at': (_NOW - timedelta(days=5)).isoformat()},
        after_json={'is_active': True, 'reactivated_at': _NOW.isoformat()},
        status=ReconciliationDeltaItemStatus.pending,
    )

    # 5. noop — NHI CI grant observed, matches kernel state exactly
    delta_noop_1 = ReconciliationDeltaItem(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.access_fact,
        operation=ReconciliationDeltaOperation.noop,
        subject_id=subjects['nhi_ci'].id,
        account_id=accounts['nhi_ci_ghe'].id,
        resource_id=resources['repo_kernel'].id,
        action_id=2,  # write
        effect='allow',
        natural_key_hash=_content_hash('nhi_ci_ghe', str(resources['repo_kernel'].id), 'write'),
        status=ReconciliationDeltaItemStatus.pending,
    )

    # 6. noop — senior engineer gui write — unchanged
    delta_noop_2 = ReconciliationDeltaItem(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.access_fact,
        operation=ReconciliationDeltaOperation.noop,
        subject_id=subjects['senior_engineer'].id,
        account_id=accounts['se_ghe'].id,
        resource_id=resources['repo_gui'].id,
        action_id=2,  # write
        effect='allow',
        natural_key_hash=_content_hash('se_ghe', str(resources['repo_gui'].id), 'write'),
        status=ReconciliationDeltaItemStatus.pending,
    )

    session.add_all([delta_create, delta_update, delta_revoke, delta_reactivate, delta_noop_1, delta_noop_2])
    await session.flush()

    _created('ReconciliationRun GHE pending_apply: 4 pending + 2 noop delta items')
    return run


# ---------------------------------------------------------------------------
# Direction 1b: GWS reconciliation run — pending_apply, NO sync run
# ---------------------------------------------------------------------------


async def seed_reconcile_gws_pending(
    session: AsyncSession,
    gws: Application,
    subjects: dict[str, Subject],
    accounts: dict[str, Account],
    resources: dict[str, Resource],
) -> ReconciliationRun:
    """GWS reconcile run: 4 pending items (incl. 1 employee entity), no SyncApplyRun."""

    run_key = 'seed-diff-gws-pending-v1'

    r = await session.execute(
        sa.select(ReconciliationRun).where(
            ReconciliationRun.application_id == gws.id,
            ReconciliationRun.status == ReconciliationRunStatus.pending_apply,
            ReconciliationRun.error == run_key,
        )
    )
    existing = r.scalar_one_or_none()
    if existing is not None:
        _skip(f'ReconciliationRun GWS pending ({run_key})')
        return existing

    run = ReconciliationRun(
        id=uuid.uuid4(),
        application_id=gws.id,
        entity_type=ReconciliationEntityType.access_fact,
        status=ReconciliationRunStatus.pending_apply,
        started_at=_NOW - timedelta(minutes=3),
        finished_at=_NOW - timedelta(minutes=2),
        created_count=2,
        updated_count=1,
        revoked_count=1,
        unchanged_count=1,
        error=run_key,
    )
    session.add(run)
    await session.flush()

    # 1. create — new GCP project role for senior engineer (phoenix project added)
    delta_create = ReconciliationDeltaItem(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.access_fact,
        operation=ReconciliationDeltaOperation.create,
        subject_id=subjects['senior_engineer'].id,
        account_id=accounts['se_gws'].id,
        resource_id=resources['project_apollo'].id,
        action_id=3,  # admin
        effect='allow',
        natural_key_hash=_content_hash('se_gws', str(resources['project_apollo'].id), 'admin'),
        source_artifact_id=uuid.uuid4(),
        after_json={
            'email': 'alexei.voronov@company.com',
            'project': 'aurelion-apollo-prod',
            'role': 'roles/editor',
            'observed_at': _NOW.isoformat(),
        },
        status=ReconciliationDeltaItemStatus.pending,
    )

    # 2. create — engineer got added to platform-infra sub-group
    delta_create_2 = ReconciliationDeltaItem(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.access_fact,
        operation=ReconciliationDeltaOperation.create,
        subject_id=subjects['engineer'].id,
        account_id=None,
        resource_id=resources['group_platform_child'].id,
        action_id=2,  # write
        effect='allow',
        natural_key_hash=_content_hash('eng_gws', str(resources['group_platform_child'].id), 'write'),
        source_artifact_id=uuid.uuid4(),
        after_json={
            'email': 'maria.sokolova@company.com',
            'group': 'platform-infra@company.com',
            'role': 'MEMBER',
        },
        status=ReconciliationDeltaItemStatus.pending,
    )

    # 3. update — senior engineer group role changed member→manager
    delta_update = ReconciliationDeltaItem(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.access_fact,
        operation=ReconciliationDeltaOperation.update,
        subject_id=subjects['senior_engineer'].id,
        account_id=accounts['se_gws'].id,
        resource_id=resources['group_platform_child'].id,
        action_id=2,  # write
        effect='allow',
        natural_key_hash=_content_hash('se_gws', str(resources['group_platform_child'].id), 'write_update'),
        source_artifact_id=uuid.uuid4(),
        existing_fact_id=uuid.uuid4(),
        before_json={'role': 'MEMBER'},
        after_json={'role': 'MANAGER'},
        status=ReconciliationDeltaItemStatus.pending,
    )

    # 4. employee entity — HR system detected title change (entity_type=employee for diversity)
    delta_employee = ReconciliationDeltaItem(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.employee,
        operation=ReconciliationDeltaOperation.update,
        entity_id=subjects['engineer'].id,  # reuse subject id as stable reference
        before_json={'title': 'Engineer', 'project': 'apollo'},
        after_json={'title': 'Senior Engineer', 'project': 'phoenix'},
        status=ReconciliationDeltaItemStatus.pending,
        reason='HR feed detected title promotion',
    )

    # 5. noop — senior engineer platform-parent group unchanged
    delta_noop = ReconciliationDeltaItem(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        entity_type=ReconciliationEntityType.access_fact,
        operation=ReconciliationDeltaOperation.noop,
        subject_id=subjects['senior_engineer'].id,
        account_id=accounts['se_gws'].id,
        resource_id=resources['group_platform_parent'].id,
        action_id=1,  # read
        effect='allow',
        natural_key_hash=_content_hash('se_gws', str(resources['group_platform_parent'].id), 'read'),
        status=ReconciliationDeltaItemStatus.pending,
    )

    session.add_all([delta_create, delta_create_2, delta_update, delta_employee, delta_noop])
    await session.flush()

    _created('ReconciliationRun GWS pending_apply: 4 pending (incl 1 employee) + 1 noop')
    return run


# ---------------------------------------------------------------------------
# Direction 1c: Partially synced — SyncApplyRun done, 1 item failed
# ---------------------------------------------------------------------------


async def seed_reconcile_partial_sync(
    session: AsyncSession,
    ghe: Application,
    subjects: dict[str, Subject],
    accounts: dict[str, Account],
    resources: dict[str, Resource],
) -> None:
    """GHE reconcile run with SyncApplyRun: 3 applied + 1 failed delta items."""

    run_key = 'seed-diff-ghe-partial-sync-v1'

    r = await session.execute(
        sa.select(ReconciliationRun).where(
            ReconciliationRun.application_id == ghe.id,
            ReconciliationRun.status == ReconciliationRunStatus.partially_applied,
            ReconciliationRun.error == run_key,
        )
    )
    existing = r.scalar_one_or_none()
    if existing is not None:
        _skip(f'ReconciliationRun GHE partial sync ({run_key})')
        return

    # Parent reconcile run — partially_applied
    run = ReconciliationRun(
        id=uuid.uuid4(),
        application_id=ghe.id,
        entity_type=ReconciliationEntityType.access_fact,
        status=ReconciliationRunStatus.partially_applied,
        started_at=_NOW - timedelta(hours=2),
        finished_at=_NOW - timedelta(hours=1, minutes=55),
        created_count=2,
        updated_count=1,
        revoked_count=1,
        unchanged_count=0,
        error=run_key,
    )
    session.add(run)
    await session.flush()

    # Delta items
    items = [
        ReconciliationDeltaItem(
            id=uuid.uuid4(),
            reconciliation_run_id=run.id,
            entity_type=ReconciliationEntityType.access_fact,
            operation=ReconciliationDeltaOperation.create,
            subject_id=subjects['manager'].id,
            account_id=None,
            resource_id=resources['repo_kernel'].id,
            action_id=1,
            effect='allow',
            natural_key_hash=_content_hash('mgr', str(resources['repo_kernel'].id), 'read'),
            source_artifact_id=uuid.uuid4(),
            after_json={'permission': 'read'},
            status=ReconciliationDeltaItemStatus.applied,
            applied_at=_NOW - timedelta(hours=1, minutes=50),
        ),
        ReconciliationDeltaItem(
            id=uuid.uuid4(),
            reconciliation_run_id=run.id,
            entity_type=ReconciliationEntityType.access_fact,
            operation=ReconciliationDeltaOperation.update,
            subject_id=subjects['manager'].id,
            account_id=None,
            resource_id=resources['repo_infra'].id,
            action_id=3,
            effect='allow',
            natural_key_hash=_content_hash('mgr', str(resources['repo_infra'].id), 'admin'),
            existing_fact_id=uuid.uuid4(),
            before_json={'permission': 'write'},
            after_json={'permission': 'admin'},
            status=ReconciliationDeltaItemStatus.applied,
            applied_at=_NOW - timedelta(hours=1, minutes=49),
        ),
        ReconciliationDeltaItem(
            id=uuid.uuid4(),
            reconciliation_run_id=run.id,
            entity_type=ReconciliationEntityType.access_fact,
            operation=ReconciliationDeltaOperation.revoke,
            subject_id=subjects['on_leave'].id,
            account_id=accounts['lv_ghe'].id,
            resource_id=resources['repo_kernel'].id,
            action_id=1,
            effect='allow',
            natural_key_hash=_content_hash('lv_ghe', str(resources['repo_kernel'].id), 'revoke-partial'),
            existing_fact_id=uuid.uuid4(),
            before_json={'is_active': True},
            after_json={'is_active': False},
            status=ReconciliationDeltaItemStatus.applied,
            applied_at=_NOW - timedelta(hours=1, minutes=48),
        ),
        ReconciliationDeltaItem(
            id=uuid.uuid4(),
            reconciliation_run_id=run.id,
            entity_type=ReconciliationEntityType.access_fact,
            operation=ReconciliationDeltaOperation.create,
            subject_id=subjects['nhi_ci'].id,
            account_id=accounts['nhi_ci_ghe'].id,
            resource_id=resources['team_platform'].id,
            action_id=2,
            effect='allow',
            natural_key_hash=_content_hash('nhi_ci_ghe', str(resources['team_platform'].id), 'create-fail'),
            source_artifact_id=uuid.uuid4(),
            after_json={'team': 'platform', 'role': 'member'},
            status=ReconciliationDeltaItemStatus.failed,
            reason='connector_timeout: target system did not respond within 30s',
        ),
    ]
    session.add_all(items)
    await session.flush()

    # SyncApplyRun — status partially_applied (completed but with failures)
    sync_run = SyncApplyRun(
        id=uuid.uuid4(),
        reconciliation_run_id=run.id,
        status=SyncApplyRunStatus.partially_applied,
        mode=SyncApplyRunMode.auto_apply,
        started_at=_NOW - timedelta(hours=1, minutes=52),
        finished_at=_NOW - timedelta(hours=1, minutes=45),
        applied_count=3,
        failed_count=1,
        requested_by='seed:auto',
    )
    session.add(sync_run)
    await session.flush()

    # SyncApplyResults for the 3 applied items
    for item in items[:3]:
        result = SyncApplyResult(
            id=uuid.uuid4(),
            sync_apply_run_id=sync_run.id,
            delta_item_id=item.id,
            status=SyncApplyResultStatus.applied,
            fact_id=uuid.uuid4(),
            snapshot_id=1000 + items.index(item),
        )
        session.add(result)
    # Result for the failed item
    fail_result = SyncApplyResult(
        id=uuid.uuid4(),
        sync_apply_run_id=sync_run.id,
        delta_item_id=items[3].id,
        status=SyncApplyResultStatus.failed,
        error='connector_timeout: target system did not respond within 30s',
    )
    session.add(fail_result)
    await session.flush()

    _created('ReconciliationRun GHE partial: 3 applied + 1 failed, SyncApplyRun=partially_applied')


# ---------------------------------------------------------------------------
# Direction 2a: AccessPlan for senior engineer — phoenix project context
# ---------------------------------------------------------------------------


async def seed_plan_senior_engineer_phoenix(
    session: AsyncSession,
    subjects: dict[str, Subject],
    resources: dict[str, Resource],
) -> None:
    """Senior engineer plan: 4 proposed items for phoenix project context."""

    se_ref = str(subjects['senior_engineer'].id)
    plan_hash = _content_hash('diff-plan-se-phoenix-v1', se_ref)

    r = await session.execute(
        sa.select(AccessPlan).where(
            AccessPlan.subject_ref == se_ref,
            AccessPlan.content_hash == plan_hash,
        )
    )
    if r.scalar_one_or_none() is not None:
        _skip('AccessPlan: senior_engineer phoenix project (proposed)')
        return

    plan = AccessPlan(
        id=uuid.uuid4(),
        subject_ref=se_ref,
        subject_type='employee',
        content_hash=plan_hash,
        status=AccessPlanStatus.active,
    )
    session.add(plan)
    await session.flush()

    # item A: account_create — new account in GWS phoenix project
    item_a = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.account_create,
        application='GWORKSPACE',
        account_ref='alexei.voronov@company.com',
        target_descriptor={'project': 'aurelion-phoenix-prod', 'action': 'create_account'},
        initiatives=[{'type': 'birthright', 'origin': 'policy_rule:platform-engineer-birthright-v1'}],
        decision_snapshot={
            'rule_id': 'platform-engineer-birthright-v1',
            'reasons': ['senior engineer joined phoenix project', 'birthright rule grants project access'],
        },
    )

    # item B: grant_role — roles/editor on phoenix project (depends on account_create)
    item_b = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.grant_role,
        application='GWORKSPACE',
        account_ref='alexei.voronov@company.com',
        target_descriptor={'project': 'aurelion-phoenix-prod', 'role': 'roles/editor'},
        initiatives=[{'type': 'birthright', 'origin': 'policy_rule:platform-engineer-birthright-v1'}],
        decision_snapshot={
            'rule_id': 'platform-engineer-birthright-v1',
            'reasons': ['phoenix project requires editor access for platform engineers'],
        },
    )

    # item C: group_add — add to phoenix-eng Google Group
    item_c = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.group_add,
        application='GWORKSPACE',
        account_ref='alexei.voronov@company.com',
        target_descriptor={'group': 'phoenix-eng@company.com', 'role': 'MEMBER'},
        initiatives=[{'type': 'birthright', 'origin': 'policy_rule:platform-engineer-birthright-v1'}],
        decision_snapshot={
            'rule_id': 'platform-engineer-birthright-v1',
            'reasons': ['phoenix-eng group membership required for project participants'],
        },
    )

    # item D: entitlement_attach — attach phoenix-deploy entitlement
    item_d = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.entitlement_attach,
        application='GHE',
        account_ref='alexei.voronov',
        target_descriptor={'repo': 'aurelion-phoenix', 'permission': 'write'},
        initiatives=[{'type': 'birthright', 'origin': 'policy_rule:platform-engineer-birthright-v1'}],
        decision_snapshot={
            'rule_id': 'platform-engineer-birthright-v1',
            'reasons': ['phoenix repo write access for platform engineers in project'],
        },
    )

    session.add_all([item_a, item_b, item_c, item_d])
    await session.flush()

    # Dependency: grant_role and group_add depend on account_create
    dep_b = PlanDependency(plan_id=plan.id, item_id=item_b.id, requires_item_id=item_a.id)
    dep_c = PlanDependency(plan_id=plan.id, item_id=item_c.id, requires_item_id=item_a.id)
    session.add_all([dep_b, dep_c])

    # Executions — all proposed
    for item in [item_a, item_b, item_c, item_d]:
        exec_ = PlanItemExecution(
            plan_id=plan.id,
            item_id=item.id,
            status=PlanItemExecutionStatus.proposed,
        )
        session.add(exec_)

    await session.flush()
    _created('AccessPlan: senior_engineer phoenix — 4 items all proposed, no apply_active')


# ---------------------------------------------------------------------------
# Direction 2b: AccessPlan for NHI key rotation
# ---------------------------------------------------------------------------


async def seed_plan_nhi_key_rotation(
    session: AsyncSession,
    subjects: dict[str, Subject],
    accounts: dict[str, Account],
) -> None:
    """NHI service account plan: key rotation — 3 proposed items, no apply."""

    nhi_ref = str(subjects['nhi_ci'].id)
    plan_hash = _content_hash('diff-plan-nhi-key-rotation-v1', nhi_ref)

    r = await session.execute(
        sa.select(AccessPlan).where(
            AccessPlan.subject_ref == nhi_ref,
            AccessPlan.content_hash == plan_hash,
        )
    )
    if r.scalar_one_or_none() is not None:
        _skip('AccessPlan: NHI CI key rotation (proposed)')
        return

    plan = AccessPlan(
        id=uuid.uuid4(),
        subject_ref=nhi_ref,
        subject_type='nhi',
        content_hash=plan_hash,
        status=AccessPlanStatus.active,
    )
    session.add(plan)
    await session.flush()

    # item A: revoke_role — revoke old credential/token
    item_a = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.revoke_role,
        application='GHE',
        account_ref='aurelion-ci-sa[bot]',
        target_descriptor={
            'credential_type': 'github_token',
            'token_id': 'tok-ci-sa-legacy-001',
            'action': 'revoke',
        },
        initiative_refs=[str(uuid.uuid5(uuid.NAMESPACE_OID, 'seed-phase19-demo-init-requested-nhi-ci'))],
        decision_snapshot={
            'rule_id': 'nhi-key-rotation-policy-v1',
            'reasons': ['token age > 90 days', 'rotation scheduled by policy'],
        },
    )

    # item B: account_create — provision new credential
    item_b = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.account_create,
        application='GHE',
        account_ref='aurelion-ci-sa[bot]',
        target_descriptor={
            'credential_type': 'github_token',
            'scopes': ['repo', 'workflow'],
            'expiry_days': 90,
        },
        initiative_refs=[str(uuid.uuid5(uuid.NAMESPACE_OID, 'seed-phase19-demo-init-requested-nhi-ci'))],
        decision_snapshot={
            'rule_id': 'nhi-key-rotation-policy-v1',
            'reasons': ['new credential required after revocation'],
        },
    )

    # item C: entitlement_attach — bind new credential to CI workflows
    item_c = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.entitlement_attach,
        application='GHE',
        account_ref='aurelion-ci-sa[bot]',
        target_descriptor={
            'workflow': 'aurelion-kernel/.github/workflows/ci.yml',
            'action': 'bind_secret',
            'secret_name': 'CI_SA_TOKEN',
        },
        initiative_refs=[str(uuid.uuid5(uuid.NAMESPACE_OID, 'seed-phase19-demo-init-requested-nhi-ci'))],
        decision_snapshot={
            'rule_id': 'nhi-key-rotation-policy-v1',
            'reasons': ['bind new credential to CI workflow secrets'],
        },
    )

    session.add_all([item_a, item_b, item_c])
    await session.flush()

    # B and C depend on A (revoke must complete first)
    dep_b = PlanDependency(plan_id=plan.id, item_id=item_b.id, requires_item_id=item_a.id)
    dep_c = PlanDependency(plan_id=plan.id, item_id=item_c.id, requires_item_id=item_b.id)
    session.add_all([dep_b, dep_c])

    for item in [item_a, item_b, item_c]:
        exec_ = PlanItemExecution(
            plan_id=plan.id,
            item_id=item.id,
            status=PlanItemExecutionStatus.proposed,
        )
        session.add(exec_)

    await session.flush()
    _created('AccessPlan: NHI CI key rotation — 3 items all proposed, no apply_active')


# ---------------------------------------------------------------------------
# Direction 2c: AccessPlan for on-leave employee — requires_confirmation, revoke
# ---------------------------------------------------------------------------


async def seed_plan_on_leave_revoke(
    session: AsyncSession,
    subjects: dict[str, Subject],
    accounts: dict[str, Account],
    resources: dict[str, Resource],
) -> None:
    """On-leave employee plan: >50% revoke, requires_confirmation=True, 3 proposed items."""

    lv_ref = str(subjects['on_leave'].id)
    plan_hash = _content_hash('diff-plan-on-leave-revoke-v1', lv_ref)

    r = await session.execute(
        sa.select(AccessPlan).where(
            AccessPlan.subject_ref == lv_ref,
            AccessPlan.content_hash == plan_hash,
        )
    )
    if r.scalar_one_or_none() is not None:
        _skip('AccessPlan: on_leave revoke requires_confirmation (proposed)')
        return

    plan = AccessPlan(
        id=uuid.uuid4(),
        subject_ref=lv_ref,
        subject_type='employee',
        content_hash=plan_hash,
        status=AccessPlanStatus.active,
        requires_confirmation=True,
    )
    session.add(plan)
    await session.flush()

    # item A: revoke_role — revoke kernel read
    item_a = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.revoke_role,
        application='GHE',
        account_ref='pavel.morozov',
        target_descriptor={'repo': 'aurelion-kernel', 'permission': 'read', 'action': 'revoke'},
        initiative_refs=[str(uuid.uuid5(uuid.NAMESPACE_OID, 'seed-phase19-demo-init-grace-onleave'))],
        decision_snapshot={
            'rule_id': 'lifecycle-on-leave-revoke-v1',
            'reasons': [
                'subject status=on_leave',
                'grace period expired',
                'revoke threshold >50% of facts',
                'requires_confirmation=true',
            ],
        },
    )

    # item B: revoke_role — revoke gui read
    item_b = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.revoke_role,
        application='GHE',
        account_ref='pavel.morozov',
        target_descriptor={'repo': 'aurelion-gui', 'permission': 'read', 'action': 'revoke'},
        initiative_refs=[str(uuid.uuid5(uuid.NAMESPACE_OID, 'seed-phase19-demo-init-grace-onleave'))],
        decision_snapshot={
            'rule_id': 'lifecycle-on-leave-revoke-v1',
            'reasons': ['subject status=on_leave', 'gui read not covered by grace'],
        },
    )

    # item C: account_disable — suspend GHE account
    item_c = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.account_disable,
        application='GHE',
        account_ref='pavel.morozov',
        target_descriptor={'username': 'pavel.morozov', 'action': 'disable'},
        initiative_refs=[str(uuid.uuid5(uuid.NAMESPACE_OID, 'seed-phase19-demo-init-grace-onleave'))],
        decision_snapshot={
            'rule_id': 'lifecycle-on-leave-revoke-v1',
            'reasons': ['disable account after all revokes confirmed'],
        },
    )

    session.add_all([item_a, item_b, item_c])
    await session.flush()

    # C depends on A and B
    dep_ca = PlanDependency(plan_id=plan.id, item_id=item_c.id, requires_item_id=item_a.id)
    dep_cb = PlanDependency(plan_id=plan.id, item_id=item_c.id, requires_item_id=item_b.id)
    session.add_all([dep_ca, dep_cb])

    for item in [item_a, item_b, item_c]:
        exec_ = PlanItemExecution(
            plan_id=plan.id,
            item_id=item.id,
            status=PlanItemExecutionStatus.proposed,
        )
        session.add(exec_)

    await session.flush()
    _created('AccessPlan: on_leave revoke requires_confirmation=True — 3 items proposed')


# ---------------------------------------------------------------------------
# Direction 2d: Partially applied plan — hung pipeline in the middle
# ---------------------------------------------------------------------------


async def seed_plan_partially_applied(
    session: AsyncSession,
    subjects: dict[str, Subject],
    accounts: dict[str, Account],
    resources: dict[str, Resource],
) -> None:
    """Engineer plan: 2 done + 1 executing + 1 failed, access_apply_active row present."""

    eng_ref = str(subjects['engineer'].id)
    plan_hash = _content_hash('diff-plan-eng-partial-apply-v1', eng_ref)

    r = await session.execute(
        sa.select(AccessPlan).where(
            AccessPlan.subject_ref == eng_ref,
            AccessPlan.content_hash == plan_hash,
        )
    )
    if r.scalar_one_or_none() is not None:
        _skip('AccessPlan: engineer partial apply (hung pipeline)')
        return

    fake_pipeline_run_id = uuid.uuid5(uuid.NAMESPACE_OID, 'seed-diff-plan-eng-pipeline-001')

    plan = AccessPlan(
        id=uuid.uuid4(),
        subject_ref=eng_ref,
        subject_type='employee',
        content_hash=plan_hash,
        status=AccessPlanStatus.active,
    )
    session.add(plan)
    await session.flush()

    # item A: grant_role — phoenix project editor (done)
    item_a = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.grant_role,
        application='GWORKSPACE',
        account_ref='maria.sokolova@company.com',
        target_descriptor={'project': 'aurelion-phoenix-prod', 'role': 'roles/viewer'},
        initiatives=[{'type': 'birthright', 'origin': 'policy_rule:platform-engineer-birthright-v1'}],
        decision_snapshot={
            'rule_id': 'platform-engineer-birthright-v1',
            'reasons': ['engineer added to phoenix project'],
        },
    )

    # item B: group_add — phoenix-eng group (done)
    item_b = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.group_add,
        application='GWORKSPACE',
        account_ref='maria.sokolova@company.com',
        target_descriptor={'group': 'phoenix-eng@company.com', 'role': 'MEMBER'},
        initiatives=[{'type': 'birthright', 'origin': 'policy_rule:platform-engineer-birthright-v1'}],
        decision_snapshot={
            'rule_id': 'platform-engineer-birthright-v1',
            'reasons': ['phoenix group membership for project participants'],
        },
    )

    # item C: entitlement_attach — phoenix repo write (executing — in progress)
    item_c = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.entitlement_attach,
        application='GHE',
        account_ref='maria.sokolova',
        target_descriptor={'repo': 'aurelion-phoenix', 'permission': 'write'},
        initiatives=[{'type': 'birthright', 'origin': 'policy_rule:platform-engineer-birthright-v1'}],
        decision_snapshot={
            'rule_id': 'platform-engineer-birthright-v1',
            'reasons': ['phoenix repo write for engineers'],
        },
    )

    # item D: grant_role — secondary project (failed)
    item_d = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        kind=PlanItemKind.grant_role,
        application='GWORKSPACE',
        account_ref='maria.sokolova@company.com',
        target_descriptor={'project': 'aurelion-staging', 'role': 'roles/editor'},
        initiatives=[{'type': 'birthright', 'origin': 'policy_rule:platform-engineer-birthright-v1'}],
        decision_snapshot={
            'rule_id': 'platform-engineer-birthright-v1',
            'reasons': ['staging project access for engineers'],
        },
    )

    session.add_all([item_a, item_b, item_c, item_d])
    await session.flush()

    # Executions: A=done, B=done, C=executing, D=failed
    exec_a = PlanItemExecution(
        plan_id=plan.id,
        item_id=item_a.id,
        status=PlanItemExecutionStatus.done,
    )
    exec_b = PlanItemExecution(
        plan_id=plan.id,
        item_id=item_b.id,
        status=PlanItemExecutionStatus.done,
    )
    exec_c = PlanItemExecution(
        plan_id=plan.id,
        item_id=item_c.id,
        status=PlanItemExecutionStatus.executing,
    )
    exec_d = PlanItemExecution(
        plan_id=plan.id,
        item_id=item_d.id,
        status=PlanItemExecutionStatus.failed,
        failure_reason=PlanItemFailureReason.verify_mismatch,
    )
    session.add_all([exec_a, exec_b, exec_c, exec_d])
    await session.flush()

    # access_apply_active row — apply is in flight (hung)
    r2 = await session.execute(sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == eng_ref))
    existing_lock = r2.scalar_one_or_none()
    if existing_lock is None:
        lock = AccessApplyActive(
            subject_ref=eng_ref,
            subject_type='employee',
            pipeline_run_id=fake_pipeline_run_id,
            plan_id=plan.id,
            started_at=_NOW - timedelta(minutes=10),
        )
        session.add(lock)
        await session.flush()
        _created('AccessApplyActive: engineer partial apply lock (hung pipeline)')
    else:
        _skip('AccessApplyActive: engineer (already locked)')

    _created('AccessPlan: engineer partial apply — 2 done + 1 executing + 1 failed')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    engine = create_async_engine(settings.postgres.dsn, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    sep = '─' * 70

    print(sep)
    print('SEED PHASE 19 DIFF STATES — Direction 1 (reconcile) + Direction 2 (plan)')
    print(sep)

    async with factory() as session:
        print('[load] Loading references from base seed...')
        ghe, gws, subjects, accounts, resources = await load_references(session)
        print(f'  GHE id={ghe.id}, GWS id={gws.id}')
        print(f'  Subjects: {list(subjects.keys())}')

        print()
        print('[Dir1a] ReconciliationRun GHE — pending_apply (6 items, no sync run)...')
        await seed_reconcile_ghe_pending(session, ghe, subjects, accounts, resources)

        print()
        print('[Dir1b] ReconciliationRun GWS — pending_apply (5 items, no sync run)...')
        await seed_reconcile_gws_pending(session, gws, subjects, accounts, resources)

        print()
        print('[Dir1c] ReconciliationRun GHE — partial sync (4 items: 3 applied + 1 failed)...')
        await seed_reconcile_partial_sync(session, ghe, subjects, accounts, resources)

        print()
        print('[Dir2a] AccessPlan senior_engineer — phoenix project (4 proposed)...')
        await seed_plan_senior_engineer_phoenix(session, subjects, resources)

        print()
        print('[Dir2b] AccessPlan NHI CI — key rotation (3 proposed)...')
        await seed_plan_nhi_key_rotation(session, subjects, accounts)

        print()
        print('[Dir2c] AccessPlan on_leave — revoke requires_confirmation (3 proposed)...')
        await seed_plan_on_leave_revoke(session, subjects, accounts, resources)

        print()
        print('[Dir2d] AccessPlan engineer — partially applied (hung pipeline)...')
        await seed_plan_partially_applied(session, subjects, accounts, resources)

        await session.commit()

    print()
    print(sep)
    print('SEED COMPLETE')
    print(sep)
    print('Direction 1 — Reconciled, sync not yet applied:')
    print('  ReconciliationRun GHE   : 6 delta items (create+update+revoke+reactivate+noop×2), status=pending_apply')
    print('  ReconciliationRun GWS   : 5 delta items (create×2+update+employee+noop), status=pending_apply')
    print('  ReconciliationRun partial: 3 applied + 1 failed, SyncApplyRun=partially_applied')
    print()
    print('Direction 2 — Planned, apply not yet started:')
    print('  AccessPlan SE phoenix   : 4 items proposed (account_create→grant_role→group_add+entitlement_attach)')
    print('  AccessPlan NHI rotation : 3 items proposed (revoke_role→account_create→entitlement_attach)')
    print('  AccessPlan on_leave     : 3 items proposed, requires_confirmation=True')
    print('  AccessPlan eng partial  : 2 done + 1 executing + 1 failed, access_apply_active lock present')
    print(sep)
    print()
    print('Verify:')
    print("  curl 'http://localhost:8000/api/v0/inventory-reconciles/runs?limit=10'")
    print('  # take a run_id from above, then:')
    print("  curl 'http://localhost:8000/api/v0/inventory-reconciles/runs/<run_id>/delta-items?status=pending'")
    print("  curl 'http://localhost:8000/api/v0/plans?status=active&limit=20'")
    print('  # SQL: SELECT status, COUNT(*) FROM plan_item_executions GROUP BY status;')
    print(sep)

    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
