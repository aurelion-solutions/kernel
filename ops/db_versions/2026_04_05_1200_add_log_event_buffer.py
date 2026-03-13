"""add log_event_buffer (short-term normalized LogEvent v2 buffer)

Revision ID: b8c9d0e1f2a3
Revises: c1d2e3f4a5b6
Create Date: 2026-04-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'log_event_buffer',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('event_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('event_type', sa.String(length=255), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('level', sa.String(length=32), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('component', sa.String(length=512), nullable=False),
        sa.Column('correlation_id', sa.Text(), nullable=False),
        sa.Column('causation_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('initiator_type', sa.String(length=32), nullable=False),
        sa.Column('initiator_id', sa.String(length=512), nullable=False),
        sa.Column('actor_type', sa.String(length=32), nullable=False),
        sa.Column('actor_id', sa.String(length=512), nullable=False),
        sa.Column('target_type', sa.String(length=32), nullable=False),
        sa.Column('target_id', sa.String(length=512), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_log_event_buffer_correlation_id',
        'log_event_buffer',
        ['correlation_id'],
        unique=False,
    )
    op.create_index(
        'ix_log_event_buffer_target_type_target_id',
        'log_event_buffer',
        ['target_type', 'target_id'],
        unique=False,
    )
    op.create_index(
        'ix_log_event_buffer_timestamp',
        'log_event_buffer',
        ['timestamp'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_log_event_buffer_timestamp', table_name='log_event_buffer')
    op.drop_index('ix_log_event_buffer_target_type_target_id', table_name='log_event_buffer')
    op.drop_index('ix_log_event_buffer_correlation_id', table_name='log_event_buffer')
    op.drop_table('log_event_buffer')
