# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Mitigation domain exceptions."""

from __future__ import annotations


class MitigationError(Exception):
    """Base class for all Mitigation slice errors."""


class MitigationNotFoundError(MitigationError):
    """Raised when a Mitigation with the given id does not exist."""

    def __init__(self, mitigation_id: int) -> None:
        self.mitigation_id = mitigation_id
        super().__init__(f'Mitigation {mitigation_id} not found')


class MitigationRuleNotFoundError(MitigationError):
    """Raised when the referenced SodRule does not exist."""

    def __init__(self, rule_id: int) -> None:
        self.rule_id = rule_id
        super().__init__(f'SodRule {rule_id} not found')


class MitigationRuleNotMitigatableError(MitigationError):
    """Raised when the referenced SodRule has mitigation_allowed=False."""

    def __init__(self, rule_id: int) -> None:
        self.rule_id = rule_id
        super().__init__(f'SodRule {rule_id} does not allow mitigation')


class MitigationControlNotFoundError(MitigationError):
    """Raised when the referenced MitigationControl does not exist."""

    def __init__(self, control_id: int) -> None:
        self.control_id = control_id
        super().__init__(f'MitigationControl {control_id} not found')


class MitigationControlInactiveError(MitigationError):
    """Raised when the referenced MitigationControl is not active."""

    def __init__(self, control_id: int) -> None:
        self.control_id = control_id
        super().__init__(f'MitigationControl {control_id} is not active')


class MitigationSubjectNotFoundError(MitigationError):
    """Raised when the referenced subject (subject_id) does not exist."""

    def __init__(self, subject_id: object) -> None:
        self.subject_id = subject_id
        super().__init__(f'Subject {subject_id} not found')


class MitigationOwnerNotFoundError(MitigationError):
    """Raised when the referenced owner (owner_id) does not exist."""

    def __init__(self, owner_id: object) -> None:
        self.owner_id = owner_id
        super().__init__(f'Owner subject {owner_id} not found')


class MitigationScopePairError(MitigationError):
    """Raised when scope_key_id and scope_value are not both set or both null."""

    def __init__(self) -> None:
        super().__init__('scope_key_id and scope_value must both be set or both be null')


class MitigationValidWindowError(MitigationError):
    """Raised when valid_until is not strictly after valid_from."""

    def __init__(self) -> None:
        super().__init__('valid_until must be strictly after valid_from when set')


class MitigationDuplicateActiveError(MitigationError):
    """Raised when an active or proposed mitigation already exists for the same scope tuple."""

    def __init__(self) -> None:
        super().__init__('An active or proposed mitigation already exists for this (rule, subject, scope) tuple')


class MitigationStatusTransitionError(MitigationError):
    """Raised when the requested status transition is not allowed."""

    def __init__(self, current: str, requested: str) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f'Cannot transition mitigation from {current!r} to {requested!r}')


class MitigationReasonRequiredError(MitigationError):
    """Raised when revoke is attempted without a reason."""

    def __init__(self) -> None:
        super().__init__('reason is required when revoking a mitigation')


class MitigationInvalidInitialStatusError(MitigationError):
    """Raised when create is attempted with an invalid initial status (expired or revoked)."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f'Initial status must be proposed or active, got {status!r}')
