"""Phase 13 Step 14 — ScanEngine: add findings_created_count and findings_reused_count to scan_runs.

Adds:
  - findings_created_count  (Integer NOT NULL DEFAULT 0, CHECK >= 0)
  - findings_reused_count   (Integer NOT NULL DEFAULT 0, CHECK >= 0)

Both mirror the existing ck_scan_runs_findings_total_nonneg pattern.
Downgrade drops the CHECK constraints and columns strictly in reverse order.
"""

import sqlalchemy as sa
from alembic import op

revision = 'c3d4e5f6a7b9'
down_revision = 'b2c3d4e5f6a8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'scan_runs',
        sa.Column(
            'findings_created_count',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )
    op.add_column(
        'scan_runs',
        sa.Column(
            'findings_reused_count',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )
    op.create_check_constraint(
        'ck_scan_runs_findings_created_count_nonneg',
        'scan_runs',
        'findings_created_count >= 0',
    )
    op.create_check_constraint(
        'ck_scan_runs_findings_reused_count_nonneg',
        'scan_runs',
        'findings_reused_count >= 0',
    )


def downgrade() -> None:
    op.drop_constraint('ck_scan_runs_findings_reused_count_nonneg', 'scan_runs', type_='check')
    op.drop_constraint('ck_scan_runs_findings_created_count_nonneg', 'scan_runs', type_='check')
    op.drop_column('scan_runs', 'findings_reused_count')
    op.drop_column('scan_runs', 'findings_created_count')
