"""Phase 14 Step 2 — LLM slice: add llm_provider enum + llm_models table.

Adds:
  - PG enum ``llm_provider`` with values: llama_cpp, openai, ollama
  - table ``llm_models`` with:
      - UUID PK (gen by app)
      - name (String 255, NOT NULL, unique)
      - description (Text, nullable)
      - provider (llm_provider enum, NOT NULL)
      - local_path (Text, nullable)
      - endpoint_url (String 2048, nullable)
      - model_ref (String 255, nullable)
      - context_window (Integer, nullable)
      - max_total_tokens (Integer, nullable)
      - default_params (JSONB, NOT NULL, server_default '{}'::jsonb)
      - secret_id (UUID, FK → secrets.id ON DELETE RESTRICT, nullable)
      - is_active (Boolean, NOT NULL, server_default true)
      - created_at / updated_at (timestamptz, server_default now())
  - indexes: ix_llm_models_provider, ix_llm_models_is_active

Downgrade drops indexes, table, and enum strictly in reverse order.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = 'e5f6a7b8c9db'
down_revision = 'd4e5f6a7b8ca'
branch_labels = None
depends_on = None

_LLM_PROVIDER_ENUM = postgresql.ENUM(
    'llama_cpp',
    'openai',
    'ollama',
    name='llm_provider',
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _LLM_PROVIDER_ENUM.create(bind, checkfirst=False)

    op.create_table(
        'llm_models',
        sa.Column('id', UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column(
            'provider',
            postgresql.ENUM(
                'llama_cpp',
                'openai',
                'ollama',
                name='llm_provider',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('local_path', sa.Text(), nullable=True),
        sa.Column('endpoint_url', sa.String(2048), nullable=True),
        sa.Column('model_ref', sa.String(255), nullable=True),
        sa.Column('context_window', sa.Integer(), nullable=True),
        sa.Column('max_total_tokens', sa.Integer(), nullable=True),
        sa.Column(
            'default_params',
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column('secret_id', UUID(as_uuid=True), nullable=True),
        sa.Column(
            'is_active',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
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
            ['secret_id'],
            ['secrets.id'],
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_llm_models_name'),
    )

    op.create_index('ix_llm_models_provider', 'llm_models', ['provider'])
    op.create_index('ix_llm_models_is_active', 'llm_models', ['is_active'])


def downgrade() -> None:
    op.drop_index('ix_llm_models_is_active', table_name='llm_models')
    op.drop_index('ix_llm_models_provider', table_name='llm_models')
    op.drop_table('llm_models')
    _LLM_PROVIDER_ENUM.drop(op.get_bind(), checkfirst=False)
