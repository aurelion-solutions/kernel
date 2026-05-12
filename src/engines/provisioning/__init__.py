# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""provisioning capability slice — public re-exports."""

from src.engines.provisioning.create_account import create_account
from src.engines.provisioning.delete_account import delete_account
from src.engines.provisioning.schemas import AccountCreateRequest

# Side-effect import: registers provisioning actions in ACTION_REGISTRY at import time.
from src.engines.provisioning import actions as _actions  # noqa: F401, E402

__all__ = [
    'create_account',
    'delete_account',
    'AccountCreateRequest',
]
