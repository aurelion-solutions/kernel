# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRule slice domain exceptions."""

from __future__ import annotations


class SodRuleError(Exception):
    """Base class for all SodRule slice errors."""


class SodRuleNotFoundError(SodRuleError):
    """Raised when a SodRule with the given id is not found."""

    def __init__(self, rule_id: int) -> None:
        self.rule_id = rule_id
        super().__init__(f'SodRule {rule_id} not found')


class SodRuleCodeAlreadyExistsError(SodRuleError):
    """Raised when a SodRule with the given code already exists."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"SodRule with code '{code}' already exists")


class SodRuleScopeInvariantError(SodRuleError):
    """Raised when scope_mode and scope_key_id are inconsistent."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class SodRuleScopeKeyNotFoundError(SodRuleError):
    """Raised when the referenced scope_key_id does not exist."""

    def __init__(self, scope_key_id: int) -> None:
        self.scope_key_id = scope_key_id
        super().__init__(f'CapabilityScopeKey {scope_key_id} not found')
