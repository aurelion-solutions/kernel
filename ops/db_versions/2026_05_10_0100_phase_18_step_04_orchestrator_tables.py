"""Phase 18 Step 4 — Pipeline orchestrator persistence tables.

Creates:
  - PG enum ``pipeline_event_waiter_status``
      (waiting, matched, expired, cancelled)
  - PG enum ``pipeline_run_status``
      (pending, running, awaiting_event, cancelling, completed, failed,
       failed_timeout, cancelled)
  - PG enum ``pipeline_trigger_source``
      (http, mq, schedule, retry)
  - PG enum ``step_run_status``
      (pending, running, awaiting_event, completed, failed, failed_timeout,
       aborted, cancelled)
  - Table ``pipeline_runs``
  - Table ``step_runs``
  - Table ``pipeline_event_waiters``

All tables are greenfield CREATEs — no pre-existing data.  Downgrade fully
reverses every object in dependency order.

Partial UNIQUE note:
  ``uq_pipeline_runs_inflight_idempotency`` is a partial index that blocks
  duplicate in-flight runs for the same (pipeline_name, pipeline_version,
  content_hash) triple while allowing retries (retry_of_run_id IS NOT NULL)
  and releasing the slot on terminal status.  The WHERE clause is authoritative
  here — the ORM model carries a reflective declaration only.
"""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a7e3b9d2f041'
down_revision = 'c2f5a8d91b04'
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Enum type helpers — create_type=False so Alembic does not attempt DDL
# implicitly; we manage CREATE/DROP explicitly below.
# Enum names are in alphabetical order for grep-ability.
# ---------------------------------------------------------------------------

_event_waiter_status = postgresql.ENUM(
    'waiting',
    'matched',
    'expired',
    'cancelled',
    name='pipeline_event_waiter_status',
    create_type=False,
)

_run_status = postgresql.ENUM(
    'pending',
    'running',
    'awaiting_event',
    'cancelling',
    'completed',
    'failed',
    'failed_timeout',
    'cancelled',
    name='pipeline_run_status',
    create_type=False,
)

_trigger_source = postgresql.ENUM(
    'http',
    'mq',
    'schedule',
    'retry',
    name='pipeline_trigger_source',
    create_type=False,
)

_step_run_status = postgresql.ENUM(
    'pending',
    'running',
    'awaiting_event',
    'completed',
    'failed',
    'failed_timeout',
    'aborted',  # forensic marker set by reclaim transaction; not used by PipelineRun
    'cancelled',
    name='step_run_status',
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create enum types (alphabetical order by name).
    _event_waiter_status.create(bind, checkfirst=False)
    _run_status.create(bind, checkfirst=False)
    _trigger_source.create(bind, checkfirst=False)
    _step_run_status.create(bind, checkfirst=False)

    # 2. pipeline_runs (self-referencing FK; must exist before step_runs).
    op.create_table(
        'pipeline_runs',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text('gen_random_uuid()'),
        ),
        sa.Column('pipeline_name', sa.String(255), nullable=False),
        sa.Column('pipeline_version', sa.Integer(), nullable=False),
        sa.Column(
            'args',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column('content_hash', sa.CHAR(64), nullable=False),
        sa.Column(
            'status',
            postgresql.ENUM(
                'pending',
                'running',
                'awaiting_event',
                'cancelling',
                'completed',
                'failed',
                'failed_timeout',
                'cancelled',
                name='pipeline_run_status',
                create_type=False,
            ),
            nullable=False,
            server_default='pending',
        ),
        sa.Column('current_step', sa.String(255), nullable=True),
        sa.Column('retry_of_run_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            'trigger_source',
            postgresql.ENUM(
                'http',
                'mq',
                'schedule',
                'retry',
                name='pipeline_trigger_source',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('worker_id', sa.String(255), nullable=True),
        sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint('id', name='pk_pipeline_runs'),
        sa.ForeignKeyConstraint(
            ['retry_of_run_id'],
            ['pipeline_runs.id'],
            ondelete='SET NULL',
            name='fk_pipeline_runs_retry_of_run_id',
        ),
    )

    # 3. step_runs (FK into pipeline_runs; must exist before pipeline_event_waiters).
    op.create_table(
        'step_runs',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text('gen_random_uuid()'),
        ),
        sa.Column('pipeline_run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('step_name', sa.String(255), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False, server_default='1'),
        sa.Column(
            'status',
            postgresql.ENUM(
                'pending',
                'running',
                'awaiting_event',
                'completed',
                'failed',
                'failed_timeout',
                'aborted',
                'cancelled',
                name='step_run_status',
                create_type=False,
            ),
            nullable=False,
            server_default='pending',
        ),
        sa.Column(
            'args',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint('id', name='pk_step_runs'),
        sa.ForeignKeyConstraint(
            ['pipeline_run_id'],
            ['pipeline_runs.id'],
            ondelete='CASCADE',
            name='fk_step_runs_pipeline_run_id',
        ),
        sa.UniqueConstraint('pipeline_run_id', 'step_name', 'attempt', name='uq_step_runs_run_step_attempt'),
    )

    # 4. pipeline_event_waiters (FK into step_runs).
    op.create_table(
        'pipeline_event_waiters',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text('gen_random_uuid()'),
        ),
        sa.Column('step_run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('event_type', sa.String(255), nullable=False),
        sa.Column(
            'match',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'status',
            postgresql.ENUM(
                'waiting',
                'matched',
                'expired',
                'cancelled',
                name='pipeline_event_waiter_status',
                create_type=False,
            ),
            nullable=False,
            server_default='waiting',
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint('id', name='pk_pipeline_event_waiters'),
        sa.ForeignKeyConstraint(
            ['step_run_id'],
            ['step_runs.id'],
            ondelete='CASCADE',
            name='fk_pipeline_event_waiters_step_run_id',
        ),
        sa.UniqueConstraint('step_run_id', name='uq_pipeline_event_waiters_step_run_id'),
    )

    # 5. Indexes (separate op.create_index calls per house style).

    # Work-acquisition scan: SELECT ... FOR UPDATE SKIP LOCKED.
    op.create_index(
        'ix_pipeline_runs_status_heartbeat_created',
        'pipeline_runs',
        ['status', 'last_heartbeat_at', 'created_at'],
    )

    # Partial UNIQUE: blocks duplicate in-flight runs; releases slot on terminal
    # status; retries (retry_of_run_id IS NOT NULL) bypass.
    op.create_index(
        'uq_pipeline_runs_inflight_idempotency',
        'pipeline_runs',
        ['pipeline_name', 'pipeline_version', 'content_hash'],
        unique=True,
        postgresql_where=sa.text(
            "retry_of_run_id IS NULL AND status IN ('pending', 'running', 'awaiting_event', 'cancelling')"
        ),
    )

    op.create_index(
        'ix_step_runs_pipeline_run_id',
        'step_runs',
        ['pipeline_run_id'],
    )

    op.create_index(
        'ix_pipeline_event_waiters_event_type',
        'pipeline_event_waiters',
        ['event_type'],
    )

    op.create_index(
        'ix_pipeline_event_waiters_expires_at',
        'pipeline_event_waiters',
        ['expires_at'],
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Drop indexes (child tables first).
    op.drop_index('ix_pipeline_event_waiters_expires_at', table_name='pipeline_event_waiters')
    op.drop_index('ix_pipeline_event_waiters_event_type', table_name='pipeline_event_waiters')
    op.drop_index('ix_step_runs_pipeline_run_id', table_name='step_runs')
    # if_exists=True is defensive: partial-UNIQUE drop is safe in PG 17, but
    # guards against a partial-failure scenario where the index was never created.
    op.drop_index('uq_pipeline_runs_inflight_idempotency', table_name='pipeline_runs', if_exists=True)
    op.drop_index('ix_pipeline_runs_status_heartbeat_created', table_name='pipeline_runs')

    # Drop tables in child-first dependency order.
    op.drop_table('pipeline_event_waiters')
    op.drop_table('step_runs')
    op.drop_table('pipeline_runs')

    # Drop enum types in reverse create order; checkfirst=True guards against
    # partial-failure scenarios.
    _step_run_status.drop(bind, checkfirst=True)
    _trigger_source.drop(bind, checkfirst=True)
    _run_status.drop(bind, checkfirst=True)
    _event_waiter_status.drop(bind, checkfirst=True)
