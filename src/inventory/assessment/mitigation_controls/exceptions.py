# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""MitigationControl slice domain exceptions."""

from __future__ import annotations


class MitigationControlError(Exception):
    """Base class for all MitigationControl slice errors."""


class MitigationControlCodeAlreadyExistsError(MitigationControlError):
    """Raised when a mitigation control with the given code already exists."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"MitigationControl with code '{code}' already exists")


class MitigationControlNotFoundError(MitigationControlError):
    """Raised when a mitigation control with the given id is not found."""

    def __init__(self, control_id: int) -> None:
        self.control_id = control_id
        super().__init__(f'MitigationControl {control_id} not found')
