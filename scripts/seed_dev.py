#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1
"""
Development seed script — Meridian Fintech, AP segregation of duties.

Run from aurelion-kernel/:
    uv run python scripts/seed_dev.py

What this creates
-----------------
Applications : SAP S/4HANA, NetSuite Finance
Subjects     : Anna Kovaleva (AP Clerk), Boris Petrov (AP Manager),
               Clara Ivanova (Finance Director), DevOps-Bot (NHI/bot)
Accounts     : linked accounts in both apps + one orphan (no subject_id)
Resources    : vendor_master, f110 payment run, MIRO invoice, NS AP ledger
Capabilities : create_vendor, approve_payment, release_payment,
               view_vendors, admin_finance
Mappings     : wire SAP action slugs to capabilities
SoD rules    : SOD_AP_001 create+approve (critical), SOD_AP_002 approve+release (high)
Grants       : Clara has create_vendor + approve_payment + release_payment
               → SOD_AP_001 and SOD_AP_002 both fire on scan

Not idempotent — intended for a clean DB (right after `alembic upgrade head`).
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Import all model modules so SQLAlchemy can resolve cross-slice FK references
import importlib
import src.capabilities
import src.inventory
import src.platform

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _pkg in (src.inventory, src.capabilities, src.platform):
    for _root in map(Path, _pkg.__path__):
        for _p in _root.rglob('models.py'):
            importlib.import_module('.'.join(_p.relative_to(_PROJECT_ROOT).with_suffix('').parts))

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_grants.models import CapabilityGrant
from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping
from src.capabilities.access_analysis.sod_rule_conditions.models import (
    SodRuleCondition,
    sod_rule_condition_capabilities,
)
from src.capabilities.access_analysis.sod_rules.models import SodRule, SodRuleScope, SodSeverity
from src.core.config import settings
from src.inventory.accounts.models import Account, AccountStatus
from src.inventory.employees.models import Employee
from src.inventory.nhi.models import NHI
from src.inventory.persons.models import Person
from src.inventory.resources.models import Resource
from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind
from src.platform.applications.models import Application

_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


async def main() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:

        # ── Applications ──────────────────────────────────────────────────────
        sap = Application(
            name='SAP S/4HANA',
            code='SAP_ERP',
            config={'connector': 'sap_hana'},
            required_connector_tags=['sap'],
        )
        netsuite = Application(
            name='NetSuite Finance',
            code='NETSUITE',
            config={'connector': 'netsuite'},
            required_connector_tags=['netsuite'],
        )
        session.add_all([sap, netsuite])
        await session.flush()

        # ── Persons + Employees ────────────────────────────────────────────────
        person_anna = Person(external_id='P-001', description='Anna Kovaleva')
        person_boris = Person(external_id='P-002', description='Boris Petrov')
        person_clara = Person(external_id='P-003', description='Clara Ivanova')
        session.add_all([person_anna, person_boris, person_clara])
        await session.flush()

        emp_anna = Employee(person_id=person_anna.id, is_locked=False)
        emp_boris = Employee(person_id=person_boris.id, is_locked=False)
        emp_clara = Employee(person_id=person_clara.id, is_locked=False)
        session.add_all([emp_anna, emp_boris, emp_clara])
        await session.flush()

        # ── NHI ───────────────────────────────────────────────────────────────
        nhi_bot = NHI(
            external_id='nhi-devops-001',
            name='DevOps CI/CD Bot',
            kind='bot',
            description='Automated deployment agent for CI/CD pipelines.',
        )
        session.add(nhi_bot)
        await session.flush()

        # ── Subjects ──────────────────────────────────────────────────────────
        anna = Subject(
            external_id='emp-001', kind=SubjectKind.employee,
            principal_employee_id=emp_anna.id, status='active',
        )
        boris = Subject(
            external_id='emp-002', kind=SubjectKind.employee,
            principal_employee_id=emp_boris.id, status='active',
        )
        clara = Subject(
            external_id='emp-003', kind=SubjectKind.employee,
            principal_employee_id=emp_clara.id, status='active',
        )
        devops_bot = Subject(
            external_id='nhi-devops-001',
            kind=SubjectKind.nhi,
            nhi_kind=SubjectNHIKind.bot,
            principal_nhi_id=nhi_bot.id,
            status='active',
        )
        session.add_all([anna, boris, clara, devops_bot])
        await session.flush()

        # ── Accounts ──────────────────────────────────────────────────────────
        anna_sap = Account(
            application_id=sap.id,
            username='anna.kovaleva',
            display_name='Anna Kovaleva',
            email='anna.kovaleva@meridian.io',
            subject_id=anna.id,
            status=AccountStatus.active,
        )
        anna_ns = Account(
            application_id=netsuite.id,
            username='anna.kovaleva@meridian.io',
            display_name='Anna Kovaleva',
            email='anna.kovaleva@meridian.io',
            subject_id=anna.id,
            status=AccountStatus.active,
        )
        boris_sap = Account(
            application_id=sap.id,
            username='boris.petrov',
            display_name='Boris Petrov',
            email='boris.petrov@meridian.io',
            subject_id=boris.id,
            status=AccountStatus.active,
        )
        # Clara is privileged — Finance Director with too much access (intentional SoD scenario)
        clara_sap = Account(
            application_id=sap.id,
            username='c.ivanova',
            display_name='Clara Ivanova',
            email='c.ivanova@meridian.io',
            subject_id=clara.id,
            status=AccountStatus.active,
            is_privileged=True,
        )
        # Orphan account — no subject_id, triggers orphan_access Finding on scan
        orphan = Account(
            application_id=sap.id,
            username='devops.bot.legacy',
            display_name='DevOps Bot (decommissioned)',
            subject_id=None,
            status=AccountStatus.active,
        )
        bot_sap = Account(
            application_id=sap.id,
            username='devops.bot',
            display_name='DevOps Bot',
            subject_id=devops_bot.id,
            status=AccountStatus.active,
        )
        session.add_all([anna_sap, anna_ns, boris_sap, clara_sap, orphan, bot_sap])
        await session.flush()

        # ── Resources ─────────────────────────────────────────────────────────
        vendor_master = Resource(
            external_id='sap/obj/vendor_master',
            application_id=sap.id,
            kind='vendor_master',
            resource_type='vendor_master',
            resource_key='sap/vendor_master',
            description='SAP Vendor Master Data (FK60 / MK01)',
        )
        payment_runs = Resource(
            external_id='sap/obj/f110',
            application_id=sap.id,
            kind='payment_run',
            resource_type='payment_run',
            resource_key='sap/f110',
            description='SAP Automatic Payment Program F110',
        )
        miro = Resource(
            external_id='sap/obj/miro',
            application_id=sap.id,
            kind='invoice_approval',
            resource_type='invoice_approval',
            resource_key='sap/miro',
            description='MIRO Invoice Verification and Approval',
        )
        ap_ledger = Resource(
            external_id='ns/ledger/ap',
            application_id=netsuite.id,
            kind='ledger_account',
            resource_type='ledger_account',
            resource_key='ns/ledger/2000',
            description='NetSuite Accounts Payable Control Account',
        )
        session.add_all([vendor_master, payment_runs, miro, ap_ledger])
        await session.flush()

        # ── Scope key (seeded by migration, grab any) ──────────────────────────
        row = await session.execute(sa.text('SELECT id FROM capability_scope_keys LIMIT 1'))
        scope_key_id: int = row.scalar_one()

        # ── Capabilities ──────────────────────────────────────────────────────
        cap_create_vendor = Capability(
            slug='create_vendor',
            name='Create Vendor',
            description='Create and modify vendor master records in the ERP.',
        )
        cap_approve_payment = Capability(
            slug='approve_payment',
            name='Approve Payment',
            description='Approve payment runs and authorise outgoing payments.',
        )
        cap_release_payment = Capability(
            slug='release_payment',
            name='Release Payment',
            description='Execute and release an approved payment run.',
        )
        cap_view_vendors = Capability(
            slug='view_vendors',
            name='View Vendors',
            description='Read-only access to vendor master data.',
        )
        cap_admin_finance = Capability(
            slug='admin_finance',
            name='Finance Admin',
            description='Full administrative access across all finance modules.',
        )
        session.add_all(
            [cap_create_vendor, cap_approve_payment, cap_release_payment,
             cap_view_vendors, cap_admin_finance]
        )
        await session.flush()

        # ── Capability Mappings ────────────────────────────────────────────────
        # Constraint: num_nonnulls(resource_id, resource_kind, resource_path_glob) = 1
        # resource_path_glob='*' = application-wide (any resource in the app)
        # resource_kind='vendor_master' = only vendor_master resources

        def _mapping(cap: Capability, **kwargs: object) -> CapabilityMapping:
            return CapabilityMapping(
                capability_id=cap.id,
                scope_key_id=scope_key_id,
                scope_value_source={'kind': 'constant', 'value': '*'},
                **kwargs,
            )

        m_create_vendor = _mapping(
            cap_create_vendor,
            application_id=sap.id,
            resource_kind='vendor_master',
            action_slug='write',
        )
        m_approve = _mapping(
            cap_approve_payment,
            application_id=sap.id,
            resource_path_glob='*',
            action_slug='approve',
        )
        m_release = _mapping(
            cap_release_payment,
            application_id=sap.id,
            resource_path_glob='*',
            action_slug='execute',
        )
        m_view = _mapping(
            cap_view_vendors,
            application_id=sap.id,
            resource_kind='vendor_master',
            action_slug='read',
        )
        m_admin = _mapping(
            cap_admin_finance,
            application_id=sap.id,
            resource_path_glob='*',
            action_slug='admin',
        )
        session.add_all([m_create_vendor, m_approve, m_release, m_view, m_admin])
        await session.flush()

        # ── SoD Rules ─────────────────────────────────────────────────────────
        rule_ap001 = SodRule(
            code='SOD_AP_001',
            name='Vendor Creation + Payment Approval',
            description=(
                'No individual may both create vendor master records and approve '
                'payment runs. Classic AP four-eyes principle (IFRS / SOX).'
            ),
            severity=SodSeverity.critical,
            scope_mode=SodRuleScope.global_,
        )
        rule_ap002 = SodRule(
            code='SOD_AP_002',
            name='Payment Approval + Payment Release',
            description='The person who approves a payment run must not also release it.',
            severity=SodSeverity.high,
            scope_mode=SodRuleScope.global_,
        )
        session.add_all([rule_ap001, rule_ap002])
        await session.flush()

        # Conditions — each rule needs both conditions to fire simultaneously
        cond_001_a = SodRuleCondition(rule_id=rule_ap001.id, name='Creates vendors', min_count=1)
        cond_001_b = SodRuleCondition(rule_id=rule_ap001.id, name='Approves payments', min_count=1)
        cond_002_a = SodRuleCondition(rule_id=rule_ap002.id, name='Approves payments', min_count=1)
        cond_002_b = SodRuleCondition(rule_id=rule_ap002.id, name='Releases payments', min_count=1)
        session.add_all([cond_001_a, cond_001_b, cond_002_a, cond_002_b])
        await session.flush()

        # Wire capabilities to conditions (M2M)
        await session.execute(
            sod_rule_condition_capabilities.insert(),
            [
                {'condition_id': cond_001_a.id, 'capability_id': cap_create_vendor.id},
                {'condition_id': cond_001_b.id, 'capability_id': cap_approve_payment.id},
                {'condition_id': cond_002_a.id, 'capability_id': cap_approve_payment.id},
                {'condition_id': cond_002_b.id, 'capability_id': cap_release_payment.id},
            ],
        )

        # ── Capability Grants (projector output, simulated directly) ───────────
        #
        #   Subject        Capabilities                    SoD status
        #   ─────────────────────────────────────────────────────────────────────
        #   Anna           create_vendor, view_vendors     clean
        #   Boris          approve_payment                 clean
        #   Clara          create_vendor + approve_payment SOD_AP_001 ← fires
        #                  + release_payment               SOD_AP_002 ← fires
        #   DevOps-Bot     (none)                          clean / no capabilities

        def _grant(
            subject_id: uuid.UUID, cap: Capability, mapping: CapabilityMapping
        ) -> CapabilityGrant:
            return CapabilityGrant(
                subject_id=subject_id,
                capability_id=cap.id,
                scope_key_id=scope_key_id,
                scope_value=None,  # global/unscoped sentinel
                application_id=sap.id,
                source_effective_grant_id=uuid.uuid4(),  # simulated — no real EAS row
                source_capability_mapping_id=mapping.id,
                observed_at=_NOW,
            )

        session.add_all([
            _grant(anna.id, cap_create_vendor, m_create_vendor),
            _grant(anna.id, cap_view_vendors, m_view),
            _grant(boris.id, cap_approve_payment, m_approve),
            _grant(clara.id, cap_create_vendor, m_create_vendor),  # ← SOD_AP_001
            _grant(clara.id, cap_approve_payment, m_approve),       # ← SOD_AP_001 + SOD_AP_002
            _grant(clara.id, cap_release_payment, m_release),       # ← SOD_AP_002
        ])
        await session.flush()

        await session.commit()

    # Print a cheat-sheet of IDs for copy-pasting into API calls
    sep = '─' * 60
    print(sep)
    print('SEED COMPLETE — Meridian Fintech')
    print(sep)
    print(f'  SAP S/4HANA id      {sap.id}')
    print(f'  NetSuite id         {netsuite.id}')
    print()
    print(f'  Anna Kovaleva       {anna.id}   (create_vendor, view_vendors — clean)')
    print(f'  Boris Petrov        {boris.id}   (approve_payment — clean)')
    print(f'  Clara Ivanova       {clara.id}   ← SOD_AP_001 + SOD_AP_002')
    print(f'  DevOps-Bot          {devops_bot.id}')
    print()
    print(f'  Orphan account      {orphan.id}   (no subject_id)')
    print()
    print(f'  SOD_AP_001 rule id  {rule_ap001.id}   (create+approve, critical)')
    print(f'  SOD_AP_002 rule id  {rule_ap002.id}   (approve+release, high)')
    print()
    print('Quick API calls to try:')
    print(f'  POST /api/v0/sod/evaluate  {{"subject_id": "{clara.id}"}}')
    print(f'  POST /api/v0/sod/evaluate  {{"subject_id": "{anna.id}"}}')
    print(f'  POST /api/v0/sod/what-if   {{"subject_id": "{anna.id}", "grants": []}}')
    print(sep)

    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
