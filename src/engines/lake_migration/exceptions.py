# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Domain exceptions for the lake_migration slice."""

from __future__ import annotations


class LakeMigrationConflictError(Exception):
    """Raised when a migration for the given dataset is already running (advisory lock conflict).

    Maps to HTTP 409.
    """

    def __init__(self, dataset: str) -> None:
        self.dataset = dataset
        super().__init__(f'Lake migration already running for dataset: {dataset}')


class LakeMigrationNotFoundError(Exception):
    """Raised when a requested LakeMigrationRun does not exist.

    Maps to HTTP 404.
    """

    def __init__(self, run_id: object) -> None:
        self.run_id = run_id
        super().__init__(f'Lake migration run not found: {run_id}')


class LakeMigrationDatasetError(Exception):
    """Raised on invalid dataset value or dataset mismatch during resume.

    Maps to HTTP 422.
    """


class LakeMigrationResumeError(Exception):
    """Raised when a completed run is targeted for resume.

    Maps to HTTP 409.
    """
