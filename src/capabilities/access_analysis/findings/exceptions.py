# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Finding slice domain exceptions."""

from __future__ import annotations

from src.capabilities.access_analysis.findings.models import FindingStatus


class FindingError(Exception):
    """Base class for all Finding slice errors."""


class FindingNotFoundError(FindingError):
    """Raised when a Finding with the given id is not found."""

    def __init__(self, finding_id: int) -> None:
        self.finding_id = finding_id
        super().__init__(f'Finding {finding_id} not found')


class FindingStatusTransitionError(FindingError):
    """Raised when an illegal Finding status transition is attempted."""

    def __init__(self, from_status: FindingStatus, to_status: FindingStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Illegal Finding status transition: '{from_status}' → '{to_status}'")


class FindingMissingReasonError(FindingError):
    """Raised when transitioning to 'resolved' without a status_reason."""

    def __init__(self) -> None:
        super().__init__("status_reason is required when transitioning to 'resolved'")


class FindingMitigationLinkageMissingError(FindingError):
    """Raised when transitioning to 'mitigated' without a usable active_mitigation_id.

    Neither the request payload nor the existing finding row supplies one.
    """

    def __init__(self) -> None:
        super().__init__("active_mitigation_id is required when transitioning to 'mitigated'")


class FindingMitigationNotApplicableError(FindingError):
    """Raised when the referenced mitigation exists but fails linkage validation.

    ``reason`` is one of: 'not found', 'not active', 'expired window',
    'rule/subject mismatch', 'scope mismatch'.
    """

    def __init__(self, mitigation_id: int, reason: str) -> None:
        self.mitigation_id = mitigation_id
        self.reason = reason
        super().__init__(f'Mitigation {mitigation_id} is not applicable: {reason}')
