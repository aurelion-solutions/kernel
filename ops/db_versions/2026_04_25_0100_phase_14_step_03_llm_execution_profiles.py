"""Phase 14 Step 3 — LLM slice: add llm_execution_profiles table.

Adds:
  - table ``llm_execution_profiles`` with:
      - UUID PK (gen by app)
      - name (String 255, NOT NULL, unique)
      - model_id (UUID, FK → llm_models.id ON DELETE RESTRICT, NOT NULL)
      - param_overrides (JSONB, NOT NULL, server_default '{}'::jsonb)
      - created_at / updated_at (timestamptz, server_default now())
  - index: ix_llm_execution_profiles_model_id

Downgrade drops the index and table in reverse order.
No enum work — this table references no enum.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = 'f6a7b8c9d0eb'
down_revision = 'e5f6a7b8c9db'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'llm_execution_profiles',
        sa.Column('id', UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('model_id', UUID(as_uuid=True), nullable=False),
        sa.Column(
            'param_overrides',
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
            ['model_id'],
            ['llm_models.id'],
            name='fk_llm_execution_profiles_model_id_llm_models',
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_llm_execution_profiles_name'),
    )

    op.create_index(
        'ix_llm_execution_profiles_model_id',
        'llm_execution_profiles',
        ['model_id'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_llm_execution_profiles_model_id',
        table_name='llm_execution_profiles',
    )
    op.drop_table('llm_execution_profiles')
