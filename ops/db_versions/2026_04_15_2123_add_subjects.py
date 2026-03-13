"""add subjects

Revision ID: efff209b3b85
Revises: 1300a798a2bc
Create Date: 2026-04-15 21:23:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'efff209b3b85'
down_revision: Union[str, None] = '1300a798a2bc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'subjects',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('external_id', sa.String(255), nullable=False),
        sa.Column(
            'kind',
            sa.Enum('employee', 'nhi', 'customer', name='subject_kind'),
            nullable=False,
        ),
        sa.Column(
            'nhi_kind',
            sa.Enum('service_account', 'api_key', 'bot', 'certificate', name='subject_nhi_kind'),
            nullable=True,
        ),
        sa.Column('principal_employee_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('principal_nhi_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('principal_customer_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('status', sa.String(64), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.ForeignKeyConstraint(
            ['principal_employee_id'],
            ['employees.id'],
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['principal_nhi_id'],
            ['nhis.id'],
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['principal_customer_id'],
            ['customers.id'],
            ondelete='RESTRICT',
        ),
    )

    # CHECK constraints — hand-written (autogenerate does not capture complex OR-expressions)
    op.create_check_constraint(
        'ck_subjects_principal_exactly_one',
        'subjects',
        (
            "(kind = 'employee' AND principal_employee_id IS NOT NULL"
            " AND principal_nhi_id IS NULL AND principal_customer_id IS NULL)"
            " OR (kind = 'nhi' AND principal_nhi_id IS NOT NULL"
            " AND principal_employee_id IS NULL AND principal_customer_id IS NULL)"
            " OR (kind = 'customer' AND principal_customer_id IS NOT NULL"
            " AND principal_employee_id IS NULL AND principal_nhi_id IS NULL)"
        ),
    )

    op.create_check_constraint(
        'ck_subjects_nhi_kind_consistency',
        'subjects',
        (
            "(kind = 'nhi' AND nhi_kind IS NOT NULL)"
            " OR (kind != 'nhi' AND nhi_kind IS NULL)"
        ),
    )

    op.create_check_constraint(
        'ck_subjects_status_vocabulary',
        'subjects',
        (
            "(kind = 'employee' AND status IN ('hired', 'active', 'on_leave', 'terminated'))"
            " OR (kind = 'nhi' AND status IN ('active', 'expired', 'locked'))"
            " OR (kind = 'customer' AND status IN"
            " ('registered', 'verified', 'active', 'suspended', 'banned', 'deletion_requested'))"
        ),
    )

    # Partial-unique indexes on principal FK columns
    op.create_index(
        'uq_subjects_principal_employee_id',
        'subjects',
        ['principal_employee_id'],
        unique=True,
        postgresql_where=sa.text('principal_employee_id IS NOT NULL'),
    )
    op.create_index(
        'uq_subjects_principal_nhi_id',
        'subjects',
        ['principal_nhi_id'],
        unique=True,
        postgresql_where=sa.text('principal_nhi_id IS NOT NULL'),
    )
    op.create_index(
        'uq_subjects_principal_customer_id',
        'subjects',
        ['principal_customer_id'],
        unique=True,
        postgresql_where=sa.text('principal_customer_id IS NOT NULL'),
    )

    # Composite index for list queries
    op.create_index('ix_subjects_kind_status', 'subjects', ['kind', 'status'])


def downgrade() -> None:
    op.drop_index('ix_subjects_kind_status', table_name='subjects')
    op.drop_index('uq_subjects_principal_customer_id', table_name='subjects')
    op.drop_index('uq_subjects_principal_nhi_id', table_name='subjects')
    op.drop_index('uq_subjects_principal_employee_id', table_name='subjects')
    op.drop_constraint('ck_subjects_status_vocabulary', 'subjects', type_='check')
    op.drop_constraint('ck_subjects_nhi_kind_consistency', 'subjects', type_='check')
    op.drop_constraint('ck_subjects_principal_exactly_one', 'subjects', type_='check')
    op.drop_table('subjects')

    op.execute('DROP TYPE IF EXISTS subject_nhi_kind')
    op.execute('DROP TYPE IF EXISTS subject_kind')
