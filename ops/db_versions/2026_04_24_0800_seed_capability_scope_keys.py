"""Phase 13 Step 2 (seed) — default ``CapabilityScopeKey`` vocabulary (17 codes).

Data only; no DDL. Down-migration removes only rows tagged
``created_by='system:phase_13_seed'``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b5c6d7e8f9a0'
down_revision: str | None = 'a4b5c6d7e8f9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_ROWS = [
    {
        'code': 'GLOBAL',
        'name': 'Global',
        'description': 'Capability is exercised platform-wide; no narrower scope applies.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'LEGAL_ENTITY',
        'name': 'Legal entity',
        'description': 'Scope bounded by a legal entity boundary.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'ORG_UNIT',
        'name': 'Organisational unit',
        'description': 'Scope bounded by an organisational unit.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'DEPARTMENT',
        'name': 'Department',
        'description': 'Scope bounded by a department.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'COST_CENTER',
        'name': 'Cost center',
        'description': 'Scope bounded by a cost center.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'BUSINESS_UNIT',
        'name': 'Business unit',
        'description': 'Scope bounded by a business unit.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'EMPLOYEE',
        'name': 'Employee',
        'description': 'Scope bounded by an individual employee.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'POSITION',
        'name': 'Position',
        'description': 'Scope bounded by an HR position.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'PROFILE',
        'name': 'Profile',
        'description': 'Scope bounded by a job/access profile.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'APPLICATION',
        'name': 'Application',
        'description': 'Scope bounded by an application.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'TENANT',
        'name': 'Tenant',
        'description': 'Scope bounded by a tenant.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'ENVIRONMENT',
        'name': 'Environment',
        'description': 'Scope bounded by a deployment environment (prod/staging/dev).',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'REGION',
        'name': 'Region',
        'description': 'Scope bounded by a geographic or cloud region.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'PROJECT',
        'name': 'Project',
        'description': 'Scope bounded by a project.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'PROGRAM',
        'name': 'Program',
        'description': 'Scope bounded by a program (collection of projects).',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'FINANCE_DOMAIN',
        'name': 'Finance domain',
        'description': 'Scope bounded by a finance domain.',
        'created_by': 'system:phase_13_seed',
    },
    {
        'code': 'PAYMENT_FLOW',
        'name': 'Payment flow',
        'description': 'Scope bounded by a specific payment flow.',
        'created_by': 'system:phase_13_seed',
    },
]


def upgrade() -> None:
    scope_keys_table = sa.table(
        'capability_scope_keys',
        sa.column('code', sa.String),
        sa.column('name', sa.String),
        sa.column('description', sa.Text),
        sa.column('created_by', sa.String),
    )
    op.bulk_insert(scope_keys_table, _SEED_ROWS)


def downgrade() -> None:
    op.execute("DELETE FROM capability_scope_keys WHERE created_by = 'system:phase_13_seed'")
