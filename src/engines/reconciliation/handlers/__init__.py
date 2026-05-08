# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reconciliation handler implementations.

Importing this package triggers each handler module's registration
side-effect (``register_handler(...)`` calls at module level).

New handlers go here; add one ``from . import <module>  # noqa: F401``
line per handler so registration fires at bootstrap.
"""

from . import acl_entry  # noqa: F401 — registration side-effect
from . import db_grant  # noqa: F401 — registration side-effect
from . import privilege  # noqa: F401 — registration side-effect
from . import role  # noqa: F401 — registration side-effect
from . import sap_role  # noqa: F401 — registration side-effect
