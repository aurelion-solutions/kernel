# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ScanRun slice domain exceptions."""

from __future__ import annotations

import uuid

from src.inventory.assessment.scan_runs.models import ScanRunStatus


class ScanRunError(Exception):
    """Base class for all ScanRun slice errors."""


class ScanRunNotFoundError(ScanRunError):
    """Raised when a ScanRun with the given id is not found."""

    def __init__(self, scan_run_id: int) -> None:
        self.scan_run_id = scan_run_id
        super().__init__(f'ScanRun {scan_run_id} not found')


class ScanRunStatusTransitionError(ScanRunError):
    """Raised when an illegal status transition is attempted."""

    def __init__(self, from_status: ScanRunStatus, to_status: ScanRunStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Illegal ScanRun status transition: '{from_status}' → '{to_status}'")


class ScanRunMissingErrorMessageError(ScanRunError):
    """Raised when transitioning to 'failed' without an error_message."""

    def __init__(self) -> None:
        super().__init__("error_message is required when transitioning to 'failed'")


class ScanRunSubjectNotFoundError(ScanRunError):
    """Raised when the referenced scope_subject_id does not exist."""

    def __init__(self, subject_id: uuid.UUID) -> None:
        self.subject_id = subject_id
        super().__init__(f'Subject {subject_id} not found')


class ScanRunApplicationNotFoundError(ScanRunError):
    """Raised when the referenced scope_application_id does not exist."""

    def __init__(self, application_id: uuid.UUID) -> None:
        self.application_id = application_id
        super().__init__(f'Application {application_id} not found')
