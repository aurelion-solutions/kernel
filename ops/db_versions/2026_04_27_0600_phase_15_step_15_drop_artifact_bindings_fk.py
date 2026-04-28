"""Phase 15 Step 15 — Drop FK constraint artifact_bindings.artifact_id → access_artifacts.id.

What:
    Removes the PostgreSQL FK constraint that links ``artifact_bindings.artifact_id``
    to ``access_artifacts.id``.  The column itself (UUID, NOT NULL) and all associated
    indexes / unique constraints are left completely untouched.

Why:
    Step 16 will execute ``DROP TABLE access_artifacts``.  PostgreSQL refuses to drop a
    table while an inbound FK references it.  This revision removes that blocker.

    After this migration, ``artifact_bindings.artifact_id`` becomes a **soft Iceberg
    reference**: a bare UUID pointing to a row in the ``raw.access_artifacts`` Iceberg
    table.  There is no DB-enforced referential integrity.  Service-level validation in
    ``ArtifactBindingService`` is the only remaining integrity guard — this is an
    intentional architectural trade-off, not an oversight.

Constraint name:
    Expected: ``artifact_bindings_artifact_id_fkey`` (PostgreSQL default for an unnamed
    inline ``ForeignKey``).  No ``name=`` argument was ever passed on the ORM column, and
    no ``naming_convention`` is configured on ``Base.metadata``, so both SQLAlchemy DDL
    emission and the PG default naming path converge on this value.

    Defensive runtime lookup: ``upgrade()`` uses ``sa.inspect(bind).get_foreign_keys()``
    to find the exact constraint name rather than hard-coding it.  If the actual name
    differs from the documented default (e.g. a historical explicit name in an older
    migration), the runtime inspector will still find and drop the correct constraint.
    If no matching FK exists, the function is a no-op (idempotent re-run safety).

Downgrade:
    Recreates the FK with the canonical default name and ``ON DELETE CASCADE``, mirroring
    the original ORM declaration.
"""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from alembic import op
import sqlalchemy as sa

revision: str = 'h2i3j4k5l6m7'
down_revision: str = 'g1h2i3j4k5l6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    fks = inspector.get_foreign_keys('artifact_bindings')
    matching = [
        fk
        for fk in fks
        if fk.get('constrained_columns') == ['artifact_id'] and fk.get('referred_table') == 'access_artifacts'
    ]

    if not matching:
        # FK already absent — idempotent re-run safety.
        return

    constraint_name: str = matching[0]['name']
    op.drop_constraint(constraint_name, 'artifact_bindings', type_='foreignkey')


def downgrade() -> None:
    op.create_foreign_key(
        'artifact_bindings_artifact_id_fkey',
        'artifact_bindings',
        'access_artifacts',
        ['artifact_id'],
        ['id'],
        ondelete='CASCADE',
    )
