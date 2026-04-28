"""Phase 15 Step 16b — Drop FK constraints pointing to access_facts.id.

What:
    Removes the PostgreSQL FK constraints that link:
      - ``effective_grants.source_access_fact_id``  → ``access_facts.id``
      - ``access_usage_facts.access_fact_id``        → ``access_facts.id``
      - ``initiatives.access_fact_id``               → ``access_facts.id``

    The columns themselves (UUID, NOT NULL/nullable) and all associated
    indexes / unique constraints are left completely untouched.

Why:
    This revision runs BEFORE ``i3j4k5l6m7n8`` (DROP TABLE access_facts/access_artifacts).
    PostgreSQL refuses to DROP TABLE while inbound FK constraints exist on the target table.
    Removing the three FKs here unblocks the subsequent DROP TABLE in the next revision.

    Removing the FK declarations from the ORM models AND adding this migration
    makes both paths (live DB + ``Base.metadata.create_all()`` in tests) consistent.

Constraint names:
    All three columns were declared with inline ``ForeignKey(...)`` and no
    explicit ``name=`` argument, and ``Base.metadata`` has no global naming
    convention, so PostgreSQL assigns the default names:
      - ``effective_grants_source_access_fact_id_fkey``
      - ``access_usage_facts_access_fact_id_fkey``
      - ``initiatives_access_fact_id_fkey``

    Defensive runtime lookup via ``sa.inspect(bind).get_foreign_keys()`` is
    used so the migration remains idempotent and handles any historical name
    divergence.

Downgrade:
    Recreates all three FK constraints with ``ON DELETE CASCADE``, mirroring
    the original ORM declarations.  This is only meaningful if the
    ``access_facts`` table has been restored first (Step 16 downgrade handles
    that).
"""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import sqlalchemy as sa
from alembic import op

revision: str = 'j4k5l6m7n8o9'
down_revision: str = 'h2i3j4k5l6m7'
branch_labels = None
depends_on = None


def _drop_fk_if_exists(
    bind: sa.engine.Connection,
    table_name: str,
    constrained_column: str,
    referred_table: str,
) -> None:
    """Drop the FK on *table_name.constrained_column* → *referred_table* if it exists."""
    inspector = sa.inspect(bind)
    # get_foreign_keys returns [] for tables that no longer exist; safe to call.
    try:
        fks = inspector.get_foreign_keys(table_name)
    except Exception:  # noqa: BLE001
        # Table might not exist in a partial downgrade scenario.
        return

    matching = [
        fk
        for fk in fks
        if fk.get('constrained_columns') == [constrained_column]
        and fk.get('referred_table') == referred_table
    ]

    if not matching:
        return

    constraint_name: str = matching[0]['name']
    op.drop_constraint(constraint_name, table_name, type_='foreignkey')


def upgrade() -> None:
    bind = op.get_bind()

    _drop_fk_if_exists(bind, 'effective_grants', 'source_access_fact_id', 'access_facts')
    _drop_fk_if_exists(bind, 'access_usage_facts', 'access_fact_id', 'access_facts')
    _drop_fk_if_exists(bind, 'initiatives', 'access_fact_id', 'access_facts')


def downgrade() -> None:
    op.create_foreign_key(
        'effective_grants_source_access_fact_id_fkey',
        'effective_grants',
        'access_facts',
        ['source_access_fact_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_foreign_key(
        'access_usage_facts_access_fact_id_fkey',
        'access_usage_facts',
        'access_facts',
        ['access_fact_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_foreign_key(
        'initiatives_access_fact_id_fkey',
        'initiatives',
        'access_facts',
        ['access_fact_id'],
        ['id'],
        ondelete='CASCADE',
    )
