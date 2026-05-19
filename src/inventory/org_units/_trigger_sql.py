# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""PL/pgSQL trigger SQL for the org_units.is_internal per-tree consistency check.

Single source of truth imported by both the Alembic migration
``2026_05_15_2331_phase_20_kn_org_units_is_internal`` and the test conftest,
so that the two never drift on future edits.

Trigger semantics
-----------------
The trigger fires ``BEFORE INSERT OR UPDATE OF parent_id, is_internal`` on
every row.  It enforces two pairwise invariants:

1. **Parent check** — on every INSERT or UPDATE (including is_internal-only
   flips), if the row has a parent, the parent's ``is_internal`` must equal
   the row's new value.  The check runs unconditionally whenever
   ``NEW.parent_id IS NOT NULL``.

2. **Children check** — on UPDATE, if ``is_internal`` changed, no existing
   child may disagree with the new value.

Together these guarantee that every node in a connected org-unit tree shares
the same ``is_internal`` value (pairwise agreement implies tree-wide
agreement by induction).

Subtree flip constraint
-----------------------
Because the parent-side check fires on *every* UPDATE of a node that has a
parent — not only when ``parent_id`` changes — a simple single-row UPDATE
cannot flip ``is_internal`` on a non-root node independently of its parent.
**Subtree flips of >1 node are therefore not supported via plain UPDATE
statements in Phase K-N.**  To convert a multi-node subtree, drop it and
recreate it with the new value.  The trigger enforces per-row consistency on
every INSERT and UPDATE; single-row flips that contradict the parent or any
child are rejected.
"""

TRIGGER_FUNC_SQL: str = """
CREATE OR REPLACE FUNCTION org_units_assert_is_internal_consistency()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    parent_flag BOOLEAN;
BEGIN
    -- Parent check: always run when this row has a parent, regardless of
    -- whether parent_id itself changed.  A single-row flip of is_internal
    -- on a non-root node is rejected if it disagrees with the current
    -- parent value — subtree flips are not supported via plain UPDATE.
    IF NEW.parent_id IS NOT NULL THEN
        SELECT is_internal INTO parent_flag
        FROM org_units WHERE id = NEW.parent_id;
        IF parent_flag IS NOT NULL AND parent_flag <> NEW.is_internal THEN
            RAISE EXCEPTION
                'org_units.is_internal mismatch with parent (%): expected %, got %',
                NEW.parent_id, parent_flag, NEW.is_internal
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    -- Children check: on UPDATE, if is_internal flipped, reject if any
    -- existing child still holds the old value.
    IF TG_OP = 'UPDATE' AND NEW.is_internal <> OLD.is_internal THEN
        IF EXISTS (
            SELECT 1 FROM org_units
            WHERE parent_id = NEW.id AND is_internal <> NEW.is_internal
        ) THEN
            RAISE EXCEPTION
                'org_units.is_internal mismatch with at least one child of %',
                NEW.id USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""

TRIGGER_DROP_IF_EXISTS: str = 'DROP TRIGGER IF EXISTS trg_org_units_is_internal_consistency ON org_units;'

TRIGGER_CREATE_SQL: str = """
CREATE TRIGGER trg_org_units_is_internal_consistency
BEFORE INSERT OR UPDATE OF parent_id, is_internal ON org_units
FOR EACH ROW EXECUTE FUNCTION org_units_assert_is_internal_consistency();
"""
