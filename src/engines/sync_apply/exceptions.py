# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Domain exceptions for the sync_apply slice."""

from __future__ import annotations

from uuid import UUID


class SyncApplyRunNotFoundError(Exception):
    """Raised when the referenced reconciliation run does not exist.

    Translates to HTTP 404.
    """

    def __init__(self, reconciliation_run_id: UUID) -> None:
        self.reconciliation_run_id = reconciliation_run_id
        super().__init__(f'Reconciliation run {reconciliation_run_id} not found')


class SyncApplyAlreadyExecutedError(Exception):
    """Raised when an apply run for this reconciliation_run_id already exists
    in running | completed | partially_applied status.

    Translates to HTTP 409.
    """

    def __init__(self, reconciliation_run_id: UUID) -> None:
        self.reconciliation_run_id = reconciliation_run_id
        super().__init__(
            f'An apply run for reconciliation run {reconciliation_run_id} is already running or has been completed'
        )


class SyncApplyInvalidModeError(Exception):
    """Raised when an unsupported apply mode is requested.

    Translates to HTTP 422 (Pydantic validation should catch first).
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        super().__init__(f'Invalid sync-apply mode: {mode!r}')


class SyncApplyDeltaItemNotApplicableError(Exception):
    """Raised when a requested delta item is not in ``approved`` status.

    Translates to HTTP 422.
    """

    def __init__(self, item_id: UUID, status: str) -> None:
        self.item_id = item_id
        self.status = status
        super().__init__(
            f'Delta item {item_id} is not applicable (status={status!r}); '
            'only items with status=approved can be applied'
        )
