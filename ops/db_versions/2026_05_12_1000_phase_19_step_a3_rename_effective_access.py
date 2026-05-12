"""Phase 19 Step A3 — Rename engine slice: effective_access → access_effective.

This is a Python-package-only rename. The database tables for this engine are
named effective_grants / effective_grants_* (not effective_access_*), so no
table or enum renames are required.

The migration is a no-op marker so the Alembic revision chain stays continuous
and the down_revision pointer is correct for subsequent steps.

Backward compatibility is not required (prod not running, Phase 19 internal step).
"""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

from alembic import op

revision = 'c4d5e6f78901'
down_revision = 'b3c4d5e6f789'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No database changes — Python package effective_access renamed to
    # access_effective. Tables effective_grants / effective_grants_* are
    # unaffected (they were never named effective_access_*).
    pass


def downgrade() -> None:
    pass
