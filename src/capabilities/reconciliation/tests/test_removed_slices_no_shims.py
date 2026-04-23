# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Import-smoke test: confirms that roles/privileges slices and reconcilers are fully deleted.

No shims, no _legacy wrappers, no Role = None stubs. Phase 12 Step 1 hard gate.
"""

import importlib

import pytest


@pytest.mark.parametrize(
    'module_path',
    [
        'src.inventory.roles',
        'src.inventory.privileges',
        'src.capabilities.reconciliation.reconciler_role',
        'src.capabilities.reconciliation.reconciler_privilege',
    ],
)
def test_deleted_module_raises_module_not_found(module_path: str) -> None:
    """Importing a deleted module must raise ModuleNotFoundError."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_path)
