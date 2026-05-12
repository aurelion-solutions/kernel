#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1
# ruff: noqa: E402
"""
Phase 19 demo seed — Aurelion Platform demo data.

Run from aurelion-kernel/:
    uv run python scripts/seed_phase19_demo.py

What this creates
-----------------
Applications : GitHub Enterprise (G1 connector), Google Workspace (G2 connector)
Subjects     : 5 Employees + 2 NHIs (Senior Engineer, Engineer, Contractor,
               Manager, On-Leave, Terminated; CI Service Account, Expired SA)
Accounts     : in both apps, varied statuses
Resources    : 9 objects across both apps (repos, drives, groups, roles, projects)
Actions      : ensures 'read', 'write', 'admin' exist (seed if missing)
Access Artifacts : 20 raw observed grants via POST /access-artifacts/bulk
Access Facts     : 12 lake facts via inventory_sync.lake_writer.append_single_fact_row
Initiatives  : 8 PG rows (birthright ×3, requested ×2, delegated ×1, grace ×1,
               scheduled/future ×1)
AccessPlans  : 5 plans with items + executions + deps

Idempotent: skips existing rows by external_id / unique key. Safe to re-run.
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

# Bootstrap secret provider and settings BEFORE any src.* imports that read config.
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

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from src.engines.access_plan.models import (
    AccessPlan,
    AccessPlanStatus,
    PlanDependency,
    PlanInvalidationReason,
    PlanItem,
    PlanItemExecution,
    PlanItemExecutionStatus,
    PlanItemKind,
)
from src.engines.inventory_sync.lake_writer import SingleFactRow, append_single_fact_row
from src.inventory.accounts.models import Account, AccountStatus
from src.inventory.actions.models import Action
from src.inventory.employees.models import Employee, EmployeeAttribute
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.nhi.models import NHI, NHIAttribute
from src.inventory.org_units.models import OrgUnit
from src.inventory.persons.models import Person
from src.inventory.resources.models import Resource, ResourceEnvironment, ResourcePrivilegeLevel
from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind
from src.platform.applications.models import Application
from src.platform.lake.factory import get_process_lake_catalog, get_process_lake_settings
from src.platform.logs.service import LogService

_NOW = datetime(2026, 5, 12, 10, 0, 0, tzinfo=UTC)
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
# Step 1: Applications
# ---------------------------------------------------------------------------


async def seed_applications(session: AsyncSession) -> tuple[Application, Application]:
    # GitHub Enterprise (plain structure, G1 connector)
    r = await session.execute(sa.select(Application).where(Application.code == 'GHE'))
    ghe = r.scalar_one_or_none()
    if ghe is None:
        ghe = Application(
            name='GitHub Enterprise',
            code='GHE',
            config={'connector': 'mock_g1'},
            required_connector_tags=['mock_g1'],
        )
        session.add(ghe)
        await session.flush()
        _created('Application: GitHub Enterprise (GHE)')
    else:
        _skip('Application: GitHub Enterprise (GHE)')

    # Google Workspace (hierarchical groups, G2 connector)
    r = await session.execute(sa.select(Application).where(Application.code == 'GWORKSPACE'))
    gws = r.scalar_one_or_none()
    if gws is None:
        gws = Application(
            name='Google Workspace',
            code='GWORKSPACE',
            config={'connector': 'mock_g2'},
            required_connector_tags=['mock_g2'],
        )
        session.add(gws)
        await session.flush()
        _created('Application: Google Workspace (GWORKSPACE)')
    else:
        _skip('Application: Google Workspace (GWORKSPACE)')

    return ghe, gws


# ---------------------------------------------------------------------------
# Step 2: Org Units
# ---------------------------------------------------------------------------


async def seed_org_units(session: AsyncSession) -> OrgUnit:
    r = await session.execute(sa.select(OrgUnit).where(OrgUnit.external_id == 'DEPT-PLATFORM'))
    platform_unit = r.scalar_one_or_none()
    if platform_unit is None:
        platform_unit = OrgUnit(
            external_id='DEPT-PLATFORM',
            name='Platform Engineering',
        )
        session.add(platform_unit)
        await session.flush()
        _created('OrgUnit: Platform Engineering')
    else:
        _skip('OrgUnit: Platform Engineering')
    return platform_unit


# ---------------------------------------------------------------------------
# Step 3: Employees & NHIs
# ---------------------------------------------------------------------------


async def seed_subjects(
    session: AsyncSession,
    platform_unit: OrgUnit,
) -> dict[str, Subject]:
    """Return {name: Subject} mapping for all seeded subjects."""

    subjects: dict[str, Subject] = {}

    # ---- Persons + Employees ----
    employee_specs = [
        {
            'ext': 'person-se-001',
            'name': 'Alexei Voronov',
            'emp_ext': 'emp-se-001',
            'subj_ext': 'subj-emp-se-001',
            'status': 'active',
            'locked': False,
            'attrs': {'employment_status': 'active', 'title': 'Senior Engineer'},
            'org_unit': True,
            'key': 'senior_engineer',
        },
        {
            'ext': 'person-eng-001',
            'name': 'Maria Sokolova',
            'emp_ext': 'emp-eng-001',
            'subj_ext': 'subj-emp-eng-001',
            'status': 'active',
            'locked': False,
            'attrs': {'employment_status': 'active', 'title': 'Engineer', 'project': 'apollo'},
            'org_unit': True,
            'key': 'engineer',
        },
        {
            'ext': 'person-ctr-001',
            'name': 'Igor Nikitin',
            'emp_ext': 'emp-ctr-001',
            'subj_ext': 'subj-emp-ctr-001',
            'status': 'active',
            'locked': False,
            'attrs': {'employment_status': 'contractor'},
            'org_unit': False,
            'key': 'contractor',
        },
        {
            'ext': 'person-mgr-001',
            'name': 'Elena Kozlova',
            'emp_ext': 'emp-mgr-001',
            'subj_ext': 'subj-emp-mgr-001',
            'status': 'active',
            'locked': False,
            'attrs': {'employment_status': 'active', 'title': 'Engineering Manager', 'is_lead': 'true'},
            'org_unit': True,
            'key': 'manager',
        },
        {
            'ext': 'person-lv-001',
            'name': 'Pavel Morozov',
            'emp_ext': 'emp-lv-001',
            'subj_ext': 'subj-emp-lv-001',
            'status': 'on_leave',
            'locked': False,
            'attrs': {'employment_status': 'on_leave'},
            'org_unit': True,
            'key': 'on_leave',
        },
        {
            'ext': 'person-trm-001',
            'name': 'Dmitri Volkov',
            'emp_ext': 'emp-trm-001',
            'subj_ext': 'subj-emp-trm-001',
            'status': 'terminated',
            'locked': True,
            'attrs': {'employment_status': 'terminated'},
            'org_unit': False,
            'key': 'terminated',
        },
    ]

    for spec in employee_specs:
        # Person
        r = await session.execute(sa.select(Person).where(Person.external_id == spec['ext']))
        person = r.scalar_one_or_none()
        if person is None:
            person = Person(external_id=spec['ext'], full_name=spec['name'])
            session.add(person)
            await session.flush()
            _created(f'Person: {spec["name"]}')
        else:
            _skip(f'Person: {spec["name"]}')

        # Employee
        r = await session.execute(sa.select(Employee).where(Employee.person_id == person.id))
        emp = r.scalar_one_or_none()
        if emp is None:
            emp = Employee(
                person_id=person.id,
                is_locked=spec['locked'],
                org_unit_id=platform_unit.id if spec['org_unit'] else None,
            )
            session.add(emp)
            await session.flush()
            for k, v in spec['attrs'].items():
                session.add(EmployeeAttribute(employee_id=emp.id, key=k, value=v))
            await session.flush()
            _created(f'Employee: {spec["name"]}')
        else:
            _skip(f'Employee: {spec["name"]}')

        # Subject
        r = await session.execute(sa.select(Subject).where(Subject.external_id == spec['subj_ext']))
        subj = r.scalar_one_or_none()
        if subj is None:
            subj = Subject(
                external_id=spec['subj_ext'],
                kind=SubjectKind.employee,
                principal_employee_id=emp.id,
                status=spec['status'],
            )
            session.add(subj)
            await session.flush()
            _created(f'Subject(employee): {spec["name"]}')
        else:
            _skip(f'Subject(employee): {spec["name"]}')

        subjects[spec['key']] = subj

    # ---- NHIs ----
    # Get manager's employee for owner_employee_id
    r = await session.execute(
        sa.select(Employee).where(
            Employee.person_id
            == (await session.execute(sa.select(Person).where(Person.external_id == 'person-mgr-001'))).scalar_one().id
        )
    )
    manager_emp = r.scalar_one()

    nhi_specs = [
        {
            'ext': 'nhi-ci-sa-001',
            'name': 'aurelion-ci-sa',
            'kind': 'service_account',
            'desc': 'CI/CD service account for Aurelion platform pipelines',
            'subj_ext': 'subj-nhi-ci-001',
            'nhi_kind': SubjectNHIKind.service_account,
            'status': 'active',
            'expires_at': (_NOW + timedelta(days=180)).isoformat(),
            'locked': False,
            'key': 'nhi_ci',
        },
        {
            'ext': 'nhi-legacy-sa-001',
            'name': 'apollo-deploy-sa',
            'kind': 'service_account',
            'desc': 'Legacy Apollo deployment service account (expired)',
            'subj_ext': 'subj-nhi-legacy-001',
            'nhi_kind': SubjectNHIKind.service_account,
            'status': 'expired',
            'expires_at': (_NOW - timedelta(days=30)).isoformat(),
            'locked': False,
            'key': 'nhi_expired',
        },
    ]

    for spec in nhi_specs:
        r = await session.execute(sa.select(NHI).where(NHI.external_id == spec['ext']))
        nhi = r.scalar_one_or_none()
        if nhi is None:
            nhi = NHI(
                external_id=spec['ext'],
                name=spec['name'],
                kind=spec['kind'],
                description=spec['desc'],
                is_locked=spec['locked'],
                owner_employee_id=manager_emp.id,
            )
            session.add(nhi)
            await session.flush()
            session.add(NHIAttribute(nhi_id=nhi.id, key='expires_at', value=spec['expires_at']))
            await session.flush()
            _created(f'NHI: {spec["name"]}')
        else:
            _skip(f'NHI: {spec["name"]}')

        r = await session.execute(sa.select(Subject).where(Subject.external_id == spec['subj_ext']))
        subj = r.scalar_one_or_none()
        if subj is None:
            subj = Subject(
                external_id=spec['subj_ext'],
                kind=SubjectKind.nhi,
                nhi_kind=spec['nhi_kind'],
                principal_nhi_id=nhi.id,
                status=spec['status'],
            )
            session.add(subj)
            await session.flush()
            _created(f'Subject(nhi): {spec["name"]}')
        else:
            _skip(f'Subject(nhi): {spec["name"]}')

        subjects[spec['key']] = subj

    return subjects


# ---------------------------------------------------------------------------
# Step 4: Actions (ensure slugs exist)
# ---------------------------------------------------------------------------


async def seed_actions(session: AsyncSession) -> dict[str, int]:
    needed = ['read', 'write', 'admin', 'execute', 'approve', 'use', 'own']
    result: dict[str, int] = {}
    for slug in needed:
        r = await session.execute(sa.select(Action).where(Action.slug == slug))
        action = r.scalar_one_or_none()
        if action is None:
            action = Action(slug=slug, description=f'{slug} action')
            session.add(action)
            await session.flush()
            _created(f'Action: {slug}')
        else:
            _skip(f'Action: {slug}')
        result[slug] = action.id
    return result


# ---------------------------------------------------------------------------
# Step 5: Resources
# ---------------------------------------------------------------------------


async def seed_resources(
    session: AsyncSession,
    ghe: Application,
    gws: Application,
) -> dict[str, Resource]:
    specs = [
        # GitHub Enterprise repos
        {
            'ext': 'ghe/repos/aurelion-kernel',
            'app': ghe,
            'kind': 'repository',
            'resource_type': 'repository',
            'resource_key': 'ghe/repos/aurelion-kernel',
            'description': 'Core kernel monorepo',
            'privilege': ResourcePrivilegeLevel.write,
            'env': ResourceEnvironment.production,
            'key': 'repo_kernel',
        },
        {
            'ext': 'ghe/repos/aurelion-gui',
            'app': ghe,
            'kind': 'repository',
            'resource_type': 'repository',
            'resource_key': 'ghe/repos/aurelion-gui',
            'description': 'GUI frontend repository',
            'privilege': ResourcePrivilegeLevel.write,
            'env': ResourceEnvironment.production,
            'key': 'repo_gui',
        },
        {
            'ext': 'ghe/repos/infrastructure',
            'app': ghe,
            'kind': 'repository',
            'resource_type': 'repository',
            'resource_key': 'ghe/repos/infrastructure',
            'description': 'IaC and infrastructure configurations',
            'privilege': ResourcePrivilegeLevel.admin,
            'env': ResourceEnvironment.production,
            'key': 'repo_infra',
        },
        {
            'ext': 'ghe/orgs/aurelion/teams/platform',
            'app': ghe,
            'kind': 'team',
            'resource_type': 'team',
            'resource_key': 'ghe/teams/platform',
            'description': 'Platform team GitHub org team',
            'privilege': ResourcePrivilegeLevel.write,
            'env': ResourceEnvironment.production,
            'key': 'team_platform',
        },
        {
            'ext': 'ghe/orgs/aurelion/roles/admin',
            'app': ghe,
            'kind': 'role',
            'resource_type': 'role',
            'resource_key': 'ghe/roles/admin',
            'description': 'GitHub organization admin role',
            'privilege': ResourcePrivilegeLevel.admin,
            'env': ResourceEnvironment.production,
            'key': 'role_gh_admin',
        },
        # Google Workspace resources (hierarchical)
        {
            'ext': 'gws/drives/platform-shared',
            'app': gws,
            'kind': 'drive',
            'resource_type': 'shared_drive',
            'resource_key': 'gws/drives/platform-shared',
            'description': 'Platform team shared Google Drive',
            'privilege': ResourcePrivilegeLevel.write,
            'env': ResourceEnvironment.production,
            'key': 'drive_platform',
        },
        {
            'ext': 'gws/groups/platform-eng',
            'app': gws,
            'kind': 'group',
            'resource_type': 'google_group',
            'resource_key': 'gws/groups/platform-eng@company.com',
            'description': 'Platform Engineering Google Group (parent)',
            'privilege': ResourcePrivilegeLevel.read,
            'env': ResourceEnvironment.production,
            'key': 'group_platform_parent',
        },
        {
            'ext': 'gws/groups/platform-infra',
            'app': gws,
            'kind': 'group',
            'resource_type': 'google_group',
            'resource_key': 'gws/groups/platform-infra@company.com',
            'description': 'Platform Infra sub-group',
            'privilege': ResourcePrivilegeLevel.write,
            'env': ResourceEnvironment.production,
            'key': 'group_platform_child',
        },
        {
            'ext': 'gws/projects/apollo',
            'app': gws,
            'kind': 'project',
            'resource_type': 'gcp_project',
            'resource_key': 'gws/projects/aurelion-apollo-prod',
            'description': 'Apollo GCP project (production)',
            'privilege': ResourcePrivilegeLevel.admin,
            'env': ResourceEnvironment.production,
            'key': 'project_apollo',
        },
    ]

    resources: dict[str, Resource] = {}

    # First pass — create parent resources
    for spec in specs:
        r = await session.execute(
            sa.select(Resource).where(
                Resource.application_id == spec['app'].id,
                Resource.external_id == spec['ext'],
            )
        )
        res = r.scalar_one_or_none()
        if res is None:
            res = Resource(
                external_id=spec['ext'],
                application_id=spec['app'].id,
                kind=spec['kind'],
                resource_type=spec['resource_type'],
                resource_key=spec['resource_key'],
                description=spec['description'],
                privilege_level=spec.get('privilege'),
                environment=spec.get('env'),
            )
            session.add(res)
            await session.flush()
            _created(f'Resource: {spec["ext"]}')
        else:
            _skip(f'Resource: {spec["ext"]}')
        resources[spec['key']] = res

    # Set parent for child group (hierarchical GWS)
    child = resources['group_platform_child']
    parent = resources['group_platform_parent']
    if child.parent_id is None:
        child.parent_id = parent.id
        await session.flush()

    return resources


# ---------------------------------------------------------------------------
# Step 6: Accounts
# ---------------------------------------------------------------------------


async def seed_accounts(
    session: AsyncSession,
    subjects: dict[str, Subject],
    ghe: Application,
    gws: Application,
) -> dict[str, Account]:
    """Create accounts for subjects in both apps. Returns {key: Account}."""

    specs = [
        # Senior Engineer — both apps, active
        {
            'app': ghe,
            'username': 'alexei.voronov',
            'display_name': 'Alexei Voronov',
            'email': 'alexei.voronov@company.com',
            'subject_key': 'senior_engineer',
            'status': AccountStatus.active,
            'is_privileged': False,
            'key': 'se_ghe',
        },
        {
            'app': gws,
            'username': 'alexei.voronov@company.com',
            'display_name': 'Alexei Voronov',
            'email': 'alexei.voronov@company.com',
            'subject_key': 'senior_engineer',
            'status': AccountStatus.active,
            'is_privileged': False,
            'key': 'se_gws',
        },
        # Engineer — both apps
        {
            'app': ghe,
            'username': 'maria.sokolova',
            'display_name': 'Maria Sokolova',
            'email': 'maria.sokolova@company.com',
            'subject_key': 'engineer',
            'status': AccountStatus.active,
            'is_privileged': False,
            'key': 'eng_ghe',
        },
        {
            'app': gws,
            'username': 'maria.sokolova@company.com',
            'display_name': 'Maria Sokolova',
            'email': 'maria.sokolova@company.com',
            'subject_key': 'engineer',
            'status': AccountStatus.active,
            'is_privileged': False,
            'key': 'eng_gws',
        },
        # Contractor — GHE only (no GWS)
        {
            'app': ghe,
            'username': 'igor.nikitin.ctr',
            'display_name': 'Igor Nikitin (Contractor)',
            'email': 'igor.nikitin@external.com',
            'subject_key': 'contractor',
            'status': AccountStatus.active,
            'is_privileged': False,
            'key': 'ctr_ghe',
        },
        # Manager — both apps, privileged in GHE
        {
            'app': ghe,
            'username': 'elena.kozlova',
            'display_name': 'Elena Kozlova',
            'email': 'elena.kozlova@company.com',
            'subject_key': 'manager',
            'status': AccountStatus.active,
            'is_privileged': True,
            'key': 'mgr_ghe',
        },
        {
            'app': gws,
            'username': 'elena.kozlova@company.com',
            'display_name': 'Elena Kozlova',
            'email': 'elena.kozlova@company.com',
            'subject_key': 'manager',
            'status': AccountStatus.active,
            'is_privileged': False,
            'key': 'mgr_gws',
        },
        # On-leave — GHE suspended
        {
            'app': ghe,
            'username': 'pavel.morozov',
            'display_name': 'Pavel Morozov',
            'email': 'pavel.morozov@company.com',
            'subject_key': 'on_leave',
            'status': AccountStatus.suspended,
            'is_privileged': False,
            'key': 'lv_ghe',
        },
        # Terminated — GHE disabled
        {
            'app': ghe,
            'username': 'dmitri.volkov',
            'display_name': 'Dmitri Volkov',
            'email': 'dmitri.volkov@company.com',
            'subject_key': 'terminated',
            'status': AccountStatus.disabled,
            'is_privileged': False,
            'key': 'trm_ghe',
        },
        # NHI CI service account — GHE only
        {
            'app': ghe,
            'username': 'aurelion-ci-sa[bot]',
            'display_name': 'Aurelion CI Service Account',
            'email': None,
            'subject_key': 'nhi_ci',
            'status': AccountStatus.active,
            'is_privileged': False,
            'key': 'nhi_ci_ghe',
        },
        # NHI expired — GHE invited (stale invite)
        {
            'app': ghe,
            'username': 'apollo-deploy-sa[bot]',
            'display_name': 'Apollo Deploy SA (expired)',
            'email': None,
            'subject_key': 'nhi_expired',
            'status': AccountStatus.invited,
            'is_privileged': False,
            'key': 'nhi_exp_ghe',
        },
    ]

    accounts: dict[str, Account] = {}
    for spec in specs:
        r = await session.execute(
            sa.select(Account).where(
                Account.application_id == spec['app'].id,
                Account.username == spec['username'],
            )
        )
        acc = r.scalar_one_or_none()
        if acc is None:
            acc = Account(
                application_id=spec['app'].id,
                username=spec['username'],
                display_name=spec.get('display_name'),
                email=spec.get('email'),
                subject_id=subjects[spec['subject_key']].id,
                status=spec['status'],
                is_privileged=spec.get('is_privileged', False),
                is_active=spec['status'] == AccountStatus.active,
            )
            session.add(acc)
            await session.flush()
            _created(f'Account: {spec["username"]} ({spec["app"].code})')
        else:
            _skip(f'Account: {spec["username"]} ({spec["app"].code})')
        accounts[spec['key']] = acc

    return accounts


# ---------------------------------------------------------------------------
# Step 7: Access Artifacts (via HTTP bulk upsert)
# ---------------------------------------------------------------------------


async def seed_access_artifacts(
    ghe: Application,
    gws: Application,
    accounts: dict[str, Account],
    resources: dict[str, Resource],
) -> list[uuid.UUID]:
    """POST 20 raw observed grants to /access-artifacts/bulk. Returns artifact IDs."""

    batch_id = uuid.uuid4()
    observed = _NOW.isoformat()

    items = [
        # Senior engineer on GHE repos
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_repo_permission',
            'external_id': f'ghe-perm-{accounts["se_ghe"].id}-kernel',
            'raw_name': 'write permission on aurelion-kernel',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'alexei.voronov',
                'repo': 'aurelion-kernel',
                'permission': 'write',
                'account_id': str(accounts['se_ghe'].id),
                'resource_id': str(resources['repo_kernel'].id),
                'action': 'write',
            },
        },
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_repo_permission',
            'external_id': f'ghe-perm-{accounts["se_ghe"].id}-gui',
            'raw_name': 'write permission on aurelion-gui',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'alexei.voronov',
                'repo': 'aurelion-gui',
                'permission': 'write',
                'account_id': str(accounts['se_ghe'].id),
                'resource_id': str(resources['repo_gui'].id),
                'action': 'write',
            },
        },
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_team_membership',
            'external_id': f'ghe-team-{accounts["se_ghe"].id}-platform',
            'raw_name': 'platform team membership',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'alexei.voronov',
                'team': 'platform',
                'role': 'member',
                'account_id': str(accounts['se_ghe'].id),
                'resource_id': str(resources['team_platform'].id),
                'action': 'read',
            },
        },
        # Engineer on GHE + GWS
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_repo_permission',
            'external_id': f'ghe-perm-{accounts["eng_ghe"].id}-kernel',
            'raw_name': 'write permission on aurelion-kernel',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'maria.sokolova',
                'repo': 'aurelion-kernel',
                'permission': 'write',
                'account_id': str(accounts['eng_ghe'].id),
                'resource_id': str(resources['repo_kernel'].id),
                'action': 'write',
            },
        },
        {
            'application_id': str(gws.id),
            'artifact_type': 'google_group_membership',
            'external_id': f'gws-group-{accounts["eng_gws"].id}-platform',
            'raw_name': 'platform-eng group member',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'email': 'maria.sokolova@company.com',
                'group': 'platform-eng@company.com',
                'role': 'MEMBER',
                'account_id': str(accounts['eng_gws'].id),
                'resource_id': str(resources['group_platform_parent'].id),
                'action': 'read',
            },
        },
        {
            'application_id': str(gws.id),
            'artifact_type': 'gcp_project_role',
            'external_id': f'gws-iam-{accounts["eng_gws"].id}-apollo',
            'raw_name': 'roles/viewer on apollo project',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'email': 'maria.sokolova@company.com',
                'project': 'aurelion-apollo-prod',
                'role': 'roles/viewer',
                'account_id': str(accounts['eng_gws'].id),
                'resource_id': str(resources['project_apollo'].id),
                'action': 'read',
            },
        },
        # Contractor — limited GHE access
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_repo_permission',
            'external_id': f'ghe-perm-{accounts["ctr_ghe"].id}-gui',
            'raw_name': 'read permission on aurelion-gui',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'igor.nikitin.ctr',
                'repo': 'aurelion-gui',
                'permission': 'read',
                'account_id': str(accounts['ctr_ghe'].id),
                'resource_id': str(resources['repo_gui'].id),
                'action': 'read',
            },
        },
        # Manager — admin on infra repo + GWS drive
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_repo_permission',
            'external_id': f'ghe-perm-{accounts["mgr_ghe"].id}-infra',
            'raw_name': 'admin permission on infrastructure',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'elena.kozlova',
                'repo': 'infrastructure',
                'permission': 'admin',
                'account_id': str(accounts['mgr_ghe'].id),
                'resource_id': str(resources['repo_infra'].id),
                'action': 'admin',
            },
        },
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_org_role',
            'external_id': f'ghe-role-{accounts["mgr_ghe"].id}-admin',
            'raw_name': 'organization admin role',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'elena.kozlova',
                'org': 'aurelion',
                'role': 'admin',
                'account_id': str(accounts['mgr_ghe'].id),
                'resource_id': str(resources['role_gh_admin'].id),
                'action': 'admin',
            },
        },
        {
            'application_id': str(gws.id),
            'artifact_type': 'google_drive_permission',
            'external_id': f'gws-drive-{accounts["mgr_gws"].id}-platform',
            'raw_name': 'organizer on platform shared drive',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'email': 'elena.kozlova@company.com',
                'drive': 'platform-shared',
                'role': 'organizer',
                'account_id': str(accounts['mgr_gws'].id),
                'resource_id': str(resources['drive_platform'].id),
                'action': 'admin',
            },
        },
        # Manager GWS group membership (parent group)
        {
            'application_id': str(gws.id),
            'artifact_type': 'google_group_membership',
            'external_id': f'gws-group-{accounts["mgr_gws"].id}-platform',
            'raw_name': 'platform-eng group owner',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'email': 'elena.kozlova@company.com',
                'group': 'platform-eng@company.com',
                'role': 'OWNER',
                'account_id': str(accounts['mgr_gws'].id),
                'resource_id': str(resources['group_platform_parent'].id),
                'action': 'admin',
            },
        },
        # On-leave — had GHE read access (now suspended)
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_repo_permission',
            'external_id': f'ghe-perm-{accounts["lv_ghe"].id}-kernel',
            'raw_name': 'read permission on aurelion-kernel',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'pavel.morozov',
                'repo': 'aurelion-kernel',
                'permission': 'read',
                'account_id': str(accounts['lv_ghe'].id),
                'resource_id': str(resources['repo_kernel'].id),
                'action': 'read',
            },
        },
        # Terminated — stale access
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_repo_permission',
            'external_id': f'ghe-perm-{accounts["trm_ghe"].id}-kernel',
            'raw_name': 'write permission on aurelion-kernel (stale)',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'dmitri.volkov',
                'repo': 'aurelion-kernel',
                'permission': 'write',
                'account_id': str(accounts['trm_ghe'].id),
                'resource_id': str(resources['repo_kernel'].id),
                'action': 'write',
            },
        },
        # NHI CI — write on kernel + gui
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_repo_permission',
            'external_id': f'ghe-perm-{accounts["nhi_ci_ghe"].id}-kernel',
            'raw_name': 'write permission on aurelion-kernel (CI SA)',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'aurelion-ci-sa[bot]',
                'repo': 'aurelion-kernel',
                'permission': 'write',
                'account_id': str(accounts['nhi_ci_ghe'].id),
                'resource_id': str(resources['repo_kernel'].id),
                'action': 'write',
            },
        },
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_repo_permission',
            'external_id': f'ghe-perm-{accounts["nhi_ci_ghe"].id}-infra',
            'raw_name': 'admin permission on infrastructure (CI SA)',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'aurelion-ci-sa[bot]',
                'repo': 'infrastructure',
                'permission': 'admin',
                'account_id': str(accounts['nhi_ci_ghe'].id),
                'resource_id': str(resources['repo_infra'].id),
                'action': 'admin',
            },
        },
        # NHI expired SA — stale invite artifact
        {
            'application_id': str(ghe.id),
            'artifact_type': 'github_org_invitation',
            'external_id': f'ghe-invite-{accounts["nhi_exp_ghe"].id}',
            'raw_name': 'org invite for apollo-deploy-sa (expired)',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'login': 'apollo-deploy-sa[bot]',
                'org': 'aurelion',
                'status': 'pending',
                'account_id': str(accounts['nhi_exp_ghe'].id),
            },
        },
        # Senior engineer GWS group + project
        {
            'application_id': str(gws.id),
            'artifact_type': 'google_group_membership',
            'external_id': f'gws-group-{accounts["se_gws"].id}-infra',
            'raw_name': 'platform-infra group member',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'email': 'alexei.voronov@company.com',
                'group': 'platform-infra@company.com',
                'role': 'MEMBER',
                'account_id': str(accounts['se_gws'].id),
                'resource_id': str(resources['group_platform_child'].id),
                'action': 'write',
            },
        },
        {
            'application_id': str(gws.id),
            'artifact_type': 'gcp_project_role',
            'external_id': f'gws-iam-{accounts["se_gws"].id}-apollo',
            'raw_name': 'roles/editor on apollo project',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'email': 'alexei.voronov@company.com',
                'project': 'aurelion-apollo-prod',
                'role': 'roles/editor',
                'account_id': str(accounts['se_gws'].id),
                'resource_id': str(resources['project_apollo'].id),
                'action': 'write',
            },
        },
        # Manager GWS project admin
        {
            'application_id': str(gws.id),
            'artifact_type': 'gcp_project_role',
            'external_id': f'gws-iam-{accounts["mgr_gws"].id}-apollo-admin',
            'raw_name': 'roles/owner on apollo project',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'email': 'elena.kozlova@company.com',
                'project': 'aurelion-apollo-prod',
                'role': 'roles/owner',
                'account_id': str(accounts['mgr_gws'].id),
                'resource_id': str(resources['project_apollo'].id),
                'action': 'admin',
            },
        },
        # Infra sub-group membership for CI SA (hierarchical GWS G2 scenario)
        {
            'application_id': str(gws.id),
            'artifact_type': 'google_group_membership',
            'external_id': 'gws-group-nhi-ci-infra',
            'raw_name': 'platform-infra group member (CI SA)',
            'effect': 'allow',
            'observed_at': observed,
            'payload': {
                'email': 'aurelion-ci-sa@company.com',
                'group': 'platform-infra@company.com',
                'role': 'MEMBER',
                'action': 'write',
            },
        },
    ]

    payload = {
        'ingest_batch_id': str(batch_id),
        'items': items,
        'correlation_id': 'seed-phase19-demo',
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f'{_API_BASE}/access-artifacts/bulk', json=payload)
        if resp.status_code == 200:
            data = resp.json()
            print(f'  CREATED: {data["row_count"]} access artifacts (snapshot {data["snapshot_id"]})')
        else:
            print(f'  WARN: access artifacts bulk returned {resp.status_code}: {resp.text[:200]}')

    # Return generated UUIDs for artifacts (external_id-based, for lake facts)
    return [uuid.uuid4() for _ in items]  # synthetic IDs, lake facts are independent


# ---------------------------------------------------------------------------
# Step 8: Access Facts (via lake_writer directly)
# ---------------------------------------------------------------------------


async def seed_access_facts(
    subjects: dict[str, Subject],
    accounts: dict[str, Account],
    resources: dict[str, Resource],
    actions: dict[str, int],
    ghe: Application,
    gws: Application,
) -> list[uuid.UUID]:
    """Write 12 access facts to normalized.access_facts lake table. Returns fact IDs."""

    try:
        catalog = get_process_lake_catalog()
    except Exception:  # noqa: BLE001 # allowed-broad: provider boundary
        print('  WARN: lake catalog not available, skipping access facts')
        return []

    # Minimal log service for lake_writer
    from src.platform.logs.schemas import LogLevel

    class _NullLog:
        def emit_safe(self, *, level: LogLevel, message: str, component: str, payload: dict) -> None:  # noqa: ARG002
            pass

    log_svc = _NullLog()  # type: ignore[assignment]

    fact_specs = [
        # Senior engineer: write on kernel repo (GHE)
        {
            'key': 'se_write_kernel',
            'subject_id': subjects['senior_engineer'].id,
            'account_id': accounts['se_ghe'].id,
            'resource_id': resources['repo_kernel'].id,
            'action_id': actions['write'],
            'effect': 'allow',
            'app_id': ghe.id,
            'subject_kind': 'employee',
        },
        # Senior engineer: write on gui repo (GHE)
        {
            'key': 'se_write_gui',
            'subject_id': subjects['senior_engineer'].id,
            'account_id': accounts['se_ghe'].id,
            'resource_id': resources['repo_gui'].id,
            'action_id': actions['write'],
            'effect': 'allow',
            'app_id': ghe.id,
            'subject_kind': 'employee',
        },
        # Senior engineer: write on apollo project (GWS)
        {
            'key': 'se_write_apollo',
            'subject_id': subjects['senior_engineer'].id,
            'account_id': accounts['se_gws'].id,
            'resource_id': resources['project_apollo'].id,
            'action_id': actions['write'],
            'effect': 'allow',
            'app_id': gws.id,
            'subject_kind': 'employee',
        },
        # Engineer: write on kernel (GHE)
        {
            'key': 'eng_write_kernel',
            'subject_id': subjects['engineer'].id,
            'account_id': accounts['eng_ghe'].id,
            'resource_id': resources['repo_kernel'].id,
            'action_id': actions['write'],
            'effect': 'allow',
            'app_id': ghe.id,
            'subject_kind': 'employee',
        },
        # Engineer: read on apollo project (GWS)
        {
            'key': 'eng_read_apollo',
            'subject_id': subjects['engineer'].id,
            'account_id': accounts['eng_gws'].id,
            'resource_id': resources['project_apollo'].id,
            'action_id': actions['read'],
            'effect': 'allow',
            'app_id': gws.id,
            'subject_kind': 'employee',
        },
        # Manager: admin on infra repo (GHE)
        {
            'key': 'mgr_admin_infra',
            'subject_id': subjects['manager'].id,
            'account_id': accounts['mgr_ghe'].id,
            'resource_id': resources['repo_infra'].id,
            'action_id': actions['admin'],
            'effect': 'allow',
            'app_id': ghe.id,
            'subject_kind': 'employee',
        },
        # Manager: admin on apollo project (GWS)
        {
            'key': 'mgr_admin_apollo',
            'subject_id': subjects['manager'].id,
            'account_id': accounts['mgr_gws'].id,
            'resource_id': resources['project_apollo'].id,
            'action_id': actions['admin'],
            'effect': 'allow',
            'app_id': gws.id,
            'subject_kind': 'employee',
        },
        # Contractor: read on gui (GHE)
        {
            'key': 'ctr_read_gui',
            'subject_id': subjects['contractor'].id,
            'account_id': accounts['ctr_ghe'].id,
            'resource_id': resources['repo_gui'].id,
            'action_id': actions['read'],
            'effect': 'allow',
            'app_id': ghe.id,
            'subject_kind': 'employee',
        },
        # On-leave: read on kernel (GHE) — is_active=False (account suspended)
        {
            'key': 'lv_read_kernel',
            'subject_id': subjects['on_leave'].id,
            'account_id': accounts['lv_ghe'].id,
            'resource_id': resources['repo_kernel'].id,
            'action_id': actions['read'],
            'effect': 'allow',
            'app_id': ghe.id,
            'subject_kind': 'employee',
            'is_active': False,
            'revoked_at': _NOW,
        },
        # Terminated: write on kernel — is_active=False (revoked)
        {
            'key': 'trm_write_kernel',
            'subject_id': subjects['terminated'].id,
            'account_id': accounts['trm_ghe'].id,
            'resource_id': resources['repo_kernel'].id,
            'action_id': actions['write'],
            'effect': 'allow',
            'app_id': ghe.id,
            'subject_kind': 'employee',
            'is_active': False,
            'revoked_at': _NOW - timedelta(days=1),
        },
        # NHI CI: write on kernel (GHE)
        {
            'key': 'nhi_ci_write_kernel',
            'subject_id': subjects['nhi_ci'].id,
            'account_id': accounts['nhi_ci_ghe'].id,
            'resource_id': resources['repo_kernel'].id,
            'action_id': actions['write'],
            'effect': 'allow',
            'app_id': ghe.id,
            'subject_kind': 'nhi',
        },
        # NHI CI: admin on infra (GHE)
        {
            'key': 'nhi_ci_admin_infra',
            'subject_id': subjects['nhi_ci'].id,
            'account_id': accounts['nhi_ci_ghe'].id,
            'resource_id': resources['repo_infra'].id,
            'action_id': actions['admin'],
            'effect': 'allow',
            'app_id': ghe.id,
            'subject_kind': 'nhi',
        },
    ]

    fact_ids: list[uuid.UUID] = []
    created_count = 0
    skipped_count = 0

    # Build set of existing (subject_id, resource_id, action_id) for idempotency
    existing_tuples: set[tuple[str, str, str]] = set()
    try:
        from src.platform.lake.duckdb_session import LakeSessionFactory

        _lake_settings = get_process_lake_settings()
        _pg_dsn = settings.postgres.dsn.replace('+asyncpg', '').replace('+psycopg2', '')
        _factory = LakeSessionFactory(
            settings=_lake_settings,
            log_service=log_svc,  # type: ignore[arg-type]
            pg_dsn=_pg_dsn,
        )
        _sess = _factory.acquire()
        tbl_path = _sess.iceberg_table_path('normalized', 'access_facts')
        # Filter by the specific subject_ids from our seed
        seed_subject_ids = [str(s.id) for s in subjects.values()]
        placeholders = ', '.join(f"'{sid}'" for sid in seed_subject_ids)
        _sess.execute(
            f'SELECT DISTINCT CAST(subject_id AS VARCHAR), CAST(resource_id AS VARCHAR), action_id '
            f"FROM iceberg_scan('{tbl_path}') "
            f'WHERE CAST(subject_id AS VARCHAR) IN ({placeholders})'
        )
        rows = _sess.fetchall()
        existing_tuples = {(str(r[0]), str(r[1]), str(r[2])) for r in rows}
        _factory.release(_sess)
        print(f'  [idempotency] found {len(existing_tuples)} existing fact combos')
    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        print(f'  WARN: cannot check existing facts for idempotency: {exc}')

    for spec in fact_specs:
        event_key = f'seed-phase19-demo:{spec["key"]}'
        fact_id = uuid.uuid4()

        # Idempotency check by (subject_id, resource_id, action_id) tuple
        check_tuple = (
            str(spec['subject_id']).lower(),
            str(spec['resource_id']).lower(),
            str(spec['action_id']),
        )
        if check_tuple in existing_tuples:
            _skip(f'AccessFact: {spec["key"]}')
            skipped_count += 1
            continue

        row = SingleFactRow(
            subject_id=spec['subject_id'],
            account_id=spec.get('account_id'),
            resource_id=spec['resource_id'],
            action_id=str(spec['action_id']),
            effect=spec['effect'],
            is_active=spec.get('is_active', True),
            created_at=_NOW,
            application_id_denorm=str(spec['app_id']),
            subject_kind_denorm=spec['subject_kind'],
            event_key=event_key,
            observed_at=_NOW,
            revoked_at=spec.get('revoked_at'),
        )

        try:
            append_single_fact_row(row, catalog=catalog, log_service=log_svc)  # type: ignore[arg-type]
            fact_ids.append(fact_id)
            created_count += 1
            _created(f'AccessFact: {spec["key"]}')
        except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
            print(f'  WARN: failed to write access fact {spec["key"]}: {exc}')

    print(f'  AccessFacts: {created_count} created, {skipped_count} skipped')
    return fact_ids


# ---------------------------------------------------------------------------
# Step 9: Initiatives
# ---------------------------------------------------------------------------


async def seed_initiatives(
    session: AsyncSession,
    subjects: dict[str, Subject],
    resources: dict[str, Resource],
    fact_ids: list[uuid.UUID],
) -> list[Initiative]:
    """Create 8 PG initiatives with varied origins."""

    # Generate stable fake fact IDs if lake facts weren't written
    def _fact_id(idx: int) -> uuid.UUID:
        if fact_ids and idx < len(fact_ids):
            return fact_ids[idx]
        # Stable deterministic UUID so seed is idempotent
        return uuid.uuid5(uuid.NAMESPACE_OID, f'seed-phase19-demo-fact-{idx}')

    rule_birthright_id = 'rule:platform-engineer-birthright-v1'

    now_plus_2min = _NOW + timedelta(minutes=2)

    specs = [
        # Birthright for senior engineer (kernel write)
        {
            'type': InitiativeType.birthright,
            'origin': f'policy_rule:{rule_birthright_id}',
            'access_fact_id': _fact_id(0),
            'subject_ref': str(subjects['senior_engineer'].id),
            'subject_type': 'employee',
            'valid_from': _NOW - timedelta(days=90),
            'valid_until': None,
            'key': 'birthright_se_kernel',
        },
        # Birthright for engineer (kernel write)
        {
            'type': InitiativeType.birthright,
            'origin': f'policy_rule:{rule_birthright_id}',
            'access_fact_id': _fact_id(3),
            'subject_ref': str(subjects['engineer'].id),
            'subject_type': 'employee',
            'valid_from': _NOW - timedelta(days=60),
            'valid_until': None,
            'key': 'birthright_eng_kernel',
        },
        # Birthright for engineer (apollo project read)
        {
            'type': InitiativeType.birthright,
            'origin': f'policy_rule:{rule_birthright_id}',
            'access_fact_id': _fact_id(4),
            'subject_ref': str(subjects['engineer'].id),
            'subject_type': 'employee',
            'valid_from': _NOW - timedelta(days=60),
            'valid_until': None,
            'key': 'birthright_eng_apollo',
        },
        # Requested admin access for manager
        {
            'type': InitiativeType.requested,
            'origin': 'request:req-mgr-infra-admin-a1b2c3d4',
            'access_fact_id': _fact_id(5),
            'subject_ref': str(subjects['manager'].id),
            'subject_type': 'employee',
            'valid_from': _NOW - timedelta(days=30),
            'valid_until': _NOW + timedelta(days=335),
            'key': 'requested_mgr_infra',
        },
        # Requested admin access for CI NHI
        {
            'type': InitiativeType.requested,
            'origin': 'request:req-nhi-ci-infra-e5f6a7b8',
            'access_fact_id': _fact_id(11),
            'subject_ref': str(subjects['nhi_ci'].id),
            'subject_type': 'nhi',
            'valid_from': _NOW - timedelta(days=14),
            'valid_until': _NOW + timedelta(days=166),
            'key': 'requested_nhi_ci_infra',
        },
        # Delegated: manager delegated access to senior engineer on apollo
        {
            'type': InitiativeType.delegated,
            'origin': f'delegation:{subjects["manager"].id}',
            'access_fact_id': _fact_id(2),
            'subject_ref': str(subjects['senior_engineer'].id),
            'subject_type': 'employee',
            'valid_from': _NOW - timedelta(days=7),
            'valid_until': _NOW + timedelta(days=83),
            'key': 'delegated_se_apollo',
        },
        # Grace: on-leave employee retains read (grace after soft-revoke)
        {
            'type': InitiativeType.grace,
            'origin': 'grace:seed-phase19-birthright-on-leave',
            'access_fact_id': _fact_id(8),
            'subject_ref': str(subjects['on_leave'].id),
            'subject_type': 'employee',
            'valid_from': _NOW - timedelta(days=3),
            'valid_until': _NOW + timedelta(days=27),
            'key': 'grace_onleave_kernel',
        },
        # Scheduled / future: engineer will get apollo editor access in 2 minutes
        {
            'type': InitiativeType.birthright,
            'origin': f'policy_rule:{rule_birthright_id}:scheduled',
            'access_fact_id': _fact_id(4),
            'subject_ref': str(subjects['engineer'].id),
            'subject_type': 'employee',
            'valid_from': now_plus_2min,
            'valid_until': None,
            'key': 'scheduled_eng_apollo_future',
        },
    ]

    result: list[Initiative] = []
    for spec in specs:
        # Idempotency: check by (subject_ref, type, origin)
        r = await session.execute(
            sa.select(Initiative).where(
                Initiative.subject_ref == spec['subject_ref'],
                Initiative.type == spec['type'],
                Initiative.origin == spec['origin'],
            )
        )
        ini = r.scalar_one_or_none()
        if ini is None:
            ini = Initiative(
                access_fact_id=spec['access_fact_id'],
                type=spec['type'],
                origin=spec['origin'],
                valid_from=spec['valid_from'],
                valid_until=spec.get('valid_until'),
                subject_ref=spec['subject_ref'],
                subject_type=spec['subject_type'],
            )
            session.add(ini)
            await session.flush()
            _created(f'Initiative: {spec["key"]} ({spec["type"].value})')
        else:
            _skip(f'Initiative: {spec["key"]}')
        result.append(ini)

    return result


# ---------------------------------------------------------------------------
# Step 10: AccessPlans + Items + Deps + Executions
# ---------------------------------------------------------------------------


async def seed_plans(
    session: AsyncSession,
    subjects: dict[str, Subject],
    accounts: dict[str, Account],
    resources: dict[str, Resource],
    initiatives: list[Initiative],
) -> None:
    """Create 5 access plans with items, deps, and execution records."""

    async def _plan_exists(subject_ref: str, content_hash: str) -> bool:
        r = await session.execute(
            sa.select(AccessPlan).where(
                AccessPlan.subject_ref == subject_ref,
                AccessPlan.content_hash == content_hash,
            )
        )
        return r.scalar_one_or_none() is not None

    # ---- Plan 1: Active applied plan for senior engineer (G3-like) ----
    se_ref = str(subjects['senior_engineer'].id)
    p1_hash = _content_hash('plan1', se_ref, 'applied')
    if not await _plan_exists(se_ref, p1_hash):
        p1 = AccessPlan(
            id=uuid.uuid4(),
            subject_ref=se_ref,
            subject_type='employee',
            content_hash=p1_hash,
            status=AccessPlanStatus.active,
        )
        session.add(p1)
        await session.flush()

        item1 = PlanItem(
            id=uuid.uuid4(),
            plan_id=p1.id,
            kind=PlanItemKind.grant_role,
            application='GHE',
            account_ref='alexei.voronov',
            target_descriptor={'repo': 'aurelion-kernel', 'permission': 'write'},
            initiatives=[{'type': 'birthright', 'origin': 'policy_rule:rule-be-v1'}],
            decision_snapshot={'action': 'grant', 'reason': 'birthright rule matched'},
        )
        item2 = PlanItem(
            id=uuid.uuid4(),
            plan_id=p1.id,
            kind=PlanItemKind.group_add,
            application='GWORKSPACE',
            account_ref='alexei.voronov@company.com',
            target_descriptor={'group': 'platform-infra@company.com', 'role': 'MEMBER'},
            initiatives=[{'type': 'delegated', 'origin': f'delegation:{se_ref}'}],
            decision_snapshot={'action': 'grant', 'reason': 'delegated by manager'},
        )
        session.add_all([item1, item2])
        await session.flush()

        # item2 depends on item1 (need GHE account first)
        dep = PlanDependency(plan_id=p1.id, item_id=item2.id, requires_item_id=item1.id)
        session.add(dep)

        exec1 = PlanItemExecution(plan_id=p1.id, item_id=item1.id, status=PlanItemExecutionStatus.done)
        exec2 = PlanItemExecution(plan_id=p1.id, item_id=item2.id, status=PlanItemExecutionStatus.done)
        session.add_all([exec1, exec2])
        await session.flush()
        _created('AccessPlan: senior_engineer active (applied)')
    else:
        _skip('AccessPlan: senior_engineer active (applied)')

    # ---- Plan 2: Superseded plan for manager (G4-like: org changed) ----
    mgr_ref = str(subjects['manager'].id)
    p2_hash = _content_hash('plan2', mgr_ref, 'superseded')
    if not await _plan_exists(mgr_ref, p2_hash):
        p2_old = AccessPlan(
            id=uuid.uuid4(),
            subject_ref=mgr_ref,
            subject_type='employee',
            content_hash=p2_hash,
            status=AccessPlanStatus.superseded,
        )
        session.add(p2_old)
        await session.flush()

        p2_new_hash = _content_hash('plan2-new', mgr_ref, 'active')
        p2_new = AccessPlan(
            id=uuid.uuid4(),
            subject_ref=mgr_ref,
            subject_type='employee',
            content_hash=p2_new_hash,
            status=AccessPlanStatus.active,
            supersedes_plan_id=p2_old.id,
        )
        session.add(p2_new)
        await session.flush()

        item3 = PlanItem(
            id=uuid.uuid4(),
            plan_id=p2_new.id,
            kind=PlanItemKind.grant_role,
            application='GHE',
            account_ref='elena.kozlova',
            target_descriptor={'org': 'aurelion', 'role': 'admin'},
            initiatives=[{'type': 'requested', 'origin': 'request:req-admin-001'}],
            decision_snapshot={'action': 'grant', 'reason': 'manager role entitles org admin'},
        )
        session.add(item3)
        await session.flush()

        exec3 = PlanItemExecution(plan_id=p2_new.id, item_id=item3.id, status=PlanItemExecutionStatus.proposed)
        session.add(exec3)
        await session.flush()
        _created('AccessPlan: manager superseded + new active')
    else:
        _skip('AccessPlan: manager superseded + new active')

    # ---- Plan 3: Invalid plan (stale_after_apply) ----
    eng_ref = str(subjects['engineer'].id)
    p3_hash = _content_hash('plan3', eng_ref, 'invalid')
    if not await _plan_exists(eng_ref, p3_hash):
        p3 = AccessPlan(
            id=uuid.uuid4(),
            subject_ref=eng_ref,
            subject_type='employee',
            content_hash=p3_hash,
            status=AccessPlanStatus.invalid,
            invalidation_reason=PlanInvalidationReason.stale_after_apply,
        )
        session.add(p3)
        await session.flush()
        _created('AccessPlan: engineer invalid (stale_after_apply)')
    else:
        _skip('AccessPlan: engineer invalid (stale_after_apply)')

    # ---- Plan 4: Plan with requires_confirmation for terminated employee (G5) ----
    trm_ref = str(subjects['terminated'].id)
    p4_hash = _content_hash('plan4', trm_ref, 'revoke-confirmation')
    if not await _plan_exists(trm_ref, p4_hash):
        p4 = AccessPlan(
            id=uuid.uuid4(),
            subject_ref=trm_ref,
            subject_type='employee',
            content_hash=p4_hash,
            status=AccessPlanStatus.active,
            requires_confirmation=True,
        )
        session.add(p4)
        await session.flush()

        item4 = PlanItem(
            id=uuid.uuid4(),
            plan_id=p4.id,
            kind=PlanItemKind.account_disable,
            application='GHE',
            account_ref='dmitri.volkov',
            target_descriptor={'username': 'dmitri.volkov'},
            initiative_refs=[str(uuid.uuid4())],
            decision_snapshot={
                'action': 'revoke',
                'reason': 'subject terminated, >50% destructive threshold exceeded',
            },
        )
        session.add(item4)
        await session.flush()

        exec4 = PlanItemExecution(plan_id=p4.id, item_id=item4.id, status=PlanItemExecutionStatus.proposed)
        session.add(exec4)
        await session.flush()
        _created('AccessPlan: terminated employee requires_confirmation=True (G5)')
    else:
        _skip('AccessPlan: terminated employee requires_confirmation=True (G5)')

    # ---- Plan 5: NHI plan (G8) ----
    nhi_ref = str(subjects['nhi_ci'].id)
    p5_hash = _content_hash('plan5', nhi_ref, 'nhi-active')
    if not await _plan_exists(nhi_ref, p5_hash):
        p5 = AccessPlan(
            id=uuid.uuid4(),
            subject_ref=nhi_ref,
            subject_type='nhi',
            content_hash=p5_hash,
            status=AccessPlanStatus.active,
        )
        session.add(p5)
        await session.flush()

        item5a = PlanItem(
            id=uuid.uuid4(),
            plan_id=p5.id,
            kind=PlanItemKind.grant_role,
            application='GHE',
            account_ref='aurelion-ci-sa[bot]',
            target_descriptor={'repo': 'infrastructure', 'permission': 'admin'},
            initiatives=[{'type': 'requested', 'origin': 'request:req-nhi-ci-infra-001'}],
            decision_snapshot={'action': 'grant', 'reason': 'NHI CI requires infra admin for deploy'},
        )
        item5b = PlanItem(
            id=uuid.uuid4(),
            plan_id=p5.id,
            kind=PlanItemKind.entitlement_attach,
            application='GHE',
            account_ref='aurelion-ci-sa[bot]',
            target_descriptor={'repo': 'aurelion-kernel', 'permission': 'write'},
            initiatives=[{'type': 'requested', 'origin': 'request:req-nhi-ci-kernel-001'}],
            decision_snapshot={'action': 'grant', 'reason': 'NHI CI writes to kernel'},
        )
        session.add_all([item5a, item5b])
        await session.flush()

        exec5a = PlanItemExecution(plan_id=p5.id, item_id=item5a.id, status=PlanItemExecutionStatus.executing)
        exec5b = PlanItemExecution(plan_id=p5.id, item_id=item5b.id, status=PlanItemExecutionStatus.proposed)
        session.add_all([exec5a, exec5b])

        # item5b depends on item5a
        dep5 = PlanDependency(plan_id=p5.id, item_id=item5b.id, requires_item_id=item5a.id)
        session.add(dep5)
        await session.flush()
        _created('AccessPlan: NHI CI service account (G8)')
    else:
        _skip('AccessPlan: NHI CI service account (G8)')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    from src.platform.lake.catalog import get_catalog
    from src.platform.lake.config import build_lake_settings
    from src.platform.lake.duckdb_session import LakeSessionFactory
    from src.platform.lake.factory import set_process_lake_deps
    from src.platform.lake.provisioning import ensure_tables
    from src.platform.runtime_settings.service import RuntimeSettingsService

    # Bootstrap lake catalog (needed for access facts)
    try:
        # Minimal log service for catalog init
        class _NullSink:
            def send(self, *a, **kw):
                pass  # noqa: ANN001

        _null_log = LogService(sink=_NullSink())  # type: ignore[arg-type]

        _inner_engine = create_async_engine(settings.postgres.dsn, echo=False)
        _inner_factory = async_sessionmaker(_inner_engine, class_=AsyncSession, expire_on_commit=False)

        async with _inner_factory() as _sess:
            _rt_svc = RuntimeSettingsService(_sess, _null_log)
            await _rt_svc.ensure_defaults()
            await _sess.commit()

        async with _inner_factory() as _sess:
            _rt_svc = RuntimeSettingsService(_sess, _null_log)
            _runtime = await _rt_svc.load()

        _lake_settings = build_lake_settings(
            settings.postgres,
            _runtime,
            catalog_name=settings.lake.catalog_name,
            warehouse_uri=settings.lake.warehouse_uri,
            storage_provider=settings.lake.storage_provider,  # type: ignore[arg-type]
            artifacts_write_backend=settings.lake.artifacts_write_backend,  # type: ignore[arg-type]
        )
        _lake_catalog = get_catalog(_lake_settings, _null_log)
        ensure_tables(_lake_catalog, log_service=_null_log)

        _pg_dsn = settings.postgres.dsn.replace('+asyncpg', '').replace('+psycopg2', '')
        _lake_session_factory = LakeSessionFactory(
            settings=_lake_settings,
            log_service=_null_log,
            pg_dsn=_pg_dsn,
        )
        set_process_lake_deps(
            catalog=_lake_catalog,
            session_factory=_lake_session_factory,
            settings=_lake_settings,
        )
        await _inner_engine.dispose()
        print('[lake] Catalog initialized')
    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        print(f'[lake] WARNING: could not initialize catalog: {exc}')
        print('[lake] Access facts will be skipped')

    engine = create_async_engine(settings.postgres.dsn, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    sep = '─' * 70

    print(sep)
    print('SEED PHASE 19 DEMO — Aurelion Platform')
    print(sep)

    async with factory() as session:
        print('[1/9] Applications...')
        ghe, gws = await seed_applications(session)

        print('[2/9] Org Units...')
        platform_unit = await seed_org_units(session)

        print('[3/9] Subjects (employees + NHIs)...')
        subjects = await seed_subjects(session, platform_unit)

        print('[4/9] Actions...')
        actions = await seed_actions(session)

        print('[5/9] Resources...')
        resources = await seed_resources(session, ghe, gws)

        print('[6/9] Accounts...')
        accounts = await seed_accounts(session, subjects, ghe, gws)

        await session.commit()

    print('[7/9] Access Artifacts (HTTP bulk)...')
    await seed_access_artifacts(ghe, gws, accounts, resources)

    print('[8/9] Access Facts (lake writer)...')
    fact_ids = await seed_access_facts(subjects, accounts, resources, actions, ghe, gws)

    async with factory() as session:
        print('[9/10] Initiatives...')
        initiatives = await seed_initiatives(session, subjects, resources, fact_ids)

        print('[10/10] AccessPlans...')
        await seed_plans(session, subjects, accounts, resources, initiatives)

        await session.commit()

    print(sep)
    print('SEED COMPLETE')
    print(sep)
    print('  Applications  : GitHub Enterprise (GHE), Google Workspace (GWORKSPACE)')
    print('  Subjects      : 6 employees, 2 NHIs')
    print('  Accounts      : 11 (varied statuses: active, suspended, disabled, invited)')
    print('  Resources     : 9 (repos, teams, roles, drives, groups, projects)')
    print('  Access Artifs : 20 bulk upserted')
    print(f'  Access Facts  : {len(fact_ids)} lake rows')
    print('  Initiatives   : 8 (birthright×3, requested×2, delegated×1, grace×1, scheduled×1)')
    print('  AccessPlans   : 5 (active, superseded, invalid, requires_conf, NHI)')
    print(sep)
    print('Verify with:')
    print("  curl 'http://localhost:8000/api/v0/accounts?limit=50'")
    print("  curl 'http://localhost:8000/api/v0/resources?limit=50'")
    print("  curl 'http://localhost:8000/api/v0/access-artifacts?limit=50'")
    print("  curl 'http://localhost:8000/api/v0/access-facts?limit=50'")
    print("  curl 'http://localhost:8000/api/v0/initiatives?limit=50'")
    print("  curl 'http://localhost:8000/api/v0/plans?limit=20'")
    print(sep)

    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
