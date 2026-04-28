"""Phase 15 Step 1 — create iceberg_catalog PG schema.

Creates:
  - PG schema ``iceberg_catalog`` (empty; PyIceberg manages catalog tables at runtime)

The schema is provisioned by Alembic so it exists before PyIceberg connects.
PyIceberg does NOT create schemas; it only creates tables within an existing schema.

Downgrade reverses strictly:
  - DROP SCHEMA iceberg_catalog CASCADE
    (CASCADE is required because PyIceberg will have populated catalog tables at runtime;
     this downgrade is a schema-only rollback exit and destroys all catalog metadata —
     documented as irreversible without a data migration of Iceberg catalog tables.)
"""

from alembic import op

revision = '1ce9b6a5d2c1'
down_revision = 'f6a7b8c9d0eb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS iceberg_catalog')


def downgrade() -> None:
    op.execute('DROP SCHEMA IF EXISTS iceberg_catalog CASCADE')
