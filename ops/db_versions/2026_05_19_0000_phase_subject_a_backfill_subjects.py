# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Phase Subject-A — backfill Subject rows for existing principals.

Pure data migration: inserts one Subject per Employee / NHI / Customer
that does not already have one.  No schema changes.

Each INSERT is guarded by NOT EXISTS so re-running is a no-op.
gen_random_uuid() requires pgcrypto; we ensure it is available.

Revision ID: fa1b2c3d4e5f
Revises: d1e2f3a4b5c6
Create Date: 2026-05-19 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = 'fa1b2c3d4e5f'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ensure gen_random_uuid() is available (pgcrypto).
    op.execute('CREATE EXTENSION IF NOT EXISTS pgcrypto')

    # Backfill Subjects for employees (status='active').
    op.execute(
        """
        INSERT INTO subjects
            (id, external_id, kind, principal_employee_id, status, created_at, updated_at)
        SELECT
            gen_random_uuid(),
            gen_random_uuid()::text,
            'employee',
            e.id,
            'active',
            now(),
            now()
        FROM employees e
        WHERE NOT EXISTS (
            SELECT 1 FROM subjects s WHERE s.principal_employee_id = e.id
        )
        """
    )

    # Backfill Subjects for NHIs (status='active', nhi_kind='service_account').
    # The CHECK constraint ck_subjects_nhi_kind_consistency requires nhi_kind
    # to be non-null when kind='nhi'.
    op.execute(
        """
        INSERT INTO subjects
            (id, external_id, kind, nhi_kind, principal_nhi_id, status, created_at, updated_at)
        SELECT
            gen_random_uuid(),
            gen_random_uuid()::text,
            'nhi',
            'service_account',
            n.id,
            'active',
            now(),
            now()
        FROM nhis n
        WHERE NOT EXISTS (
            SELECT 1 FROM subjects s WHERE s.principal_nhi_id = n.id
        )
        """
    )

    # Backfill Subjects for customers (status='registered').
    op.execute(
        """
        INSERT INTO subjects
            (id, external_id, kind, principal_customer_id, status, created_at, updated_at)
        SELECT
            gen_random_uuid(),
            gen_random_uuid()::text,
            'customer',
            c.id,
            'registered',
            now(),
            now()
        FROM customers c
        WHERE NOT EXISTS (
            SELECT 1 FROM subjects s WHERE s.principal_customer_id = c.id
        )
        """
    )


def downgrade() -> None:
    # Backfill is non-destructive and we cannot reliably identify rows
    # created by this migration vs rows that already existed.
    # Downgrade is intentionally a no-op.
    pass
