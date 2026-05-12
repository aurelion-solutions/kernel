# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Domain exceptions for the reconciliation slice."""

from __future__ import annotations

from uuid import UUID


class ReconciliationAlreadyRunningError(Exception):
    """Raised when a reconciliation run is already in progress for the application.

    Translates to HTTP 409.
    """

    def __init__(self, application_id: UUID) -> None:
        self.application_id = application_id
        super().__init__(f'Reconciliation already running for application {application_id}')


class ReconciliationNotFoundError(Exception):
    """Raised when a reconciliation run with the given id does not exist.

    Translates to HTTP 404.
    """

    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(f'Reconciliation run {run_id} not found')


class ReconciliationModeNotImplementedError(Exception):
    """Raised when the requested reconciliation mode is not yet implemented.

    Translates to HTTP 501.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        super().__init__(f'Reconciliation mode {mode!r} is not implemented yet — see Phase 15 Step 12')


# Keep a short alias used in routes.py
AutoApplyNotImplementedError = ReconciliationModeNotImplementedError
