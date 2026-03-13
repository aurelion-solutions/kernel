# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Application domain exceptions."""


class ApplicationNotFoundError(Exception):
    """Raised when application does not exist."""


class ApplicationCodeAlreadyExistsError(Exception):
    """Raised when another Application already owns the submitted `code` (unique violation, pgcode 23505)."""
