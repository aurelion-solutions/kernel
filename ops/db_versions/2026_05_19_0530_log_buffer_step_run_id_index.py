"""Index on log_event_buffer.payload->>'step_run_id'.

Revision ID: a3b4c5d6e7f8
Revises: fa1b2c3d4e5f
Create Date: 2026-05-19 05:30:00.000000

The runner and the step-scoped log facade stamp ``step_run_id`` into the
log event payload so that emits keep their semantically meaningful
``target_id`` (plan, account, …) while still being filterable by step in
the per-step Logs UI panel. The buffer route exposes a
``payload_step_run_id`` query parameter that compiles to
``WHERE payload->>'step_run_id' = $1``. Without an index this is a full
table scan on every UI panel open.

The index is partial — it only covers rows that actually carry the side
channel — to keep the index size proportional to step-tagged emits
rather than the full buffer.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, Sequence[str], None] = 'fa1b2c3d4e5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add a partial expression index on log_event_buffer.payload->>'step_run_id'."""
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_log_event_buffer_payload_step_run_id
        ON log_event_buffer ((payload ->> 'step_run_id'))
        WHERE payload ? 'step_run_id'
        """
    )


def downgrade() -> None:
    """Drop the partial expression index."""
    op.execute('DROP INDEX IF EXISTS ix_log_event_buffer_payload_step_run_id')
