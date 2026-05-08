# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRuleCondition slice domain exceptions."""

from __future__ import annotations


class SodRuleConditionError(Exception):
    """Base class for all SodRuleCondition slice errors."""


class SodRuleConditionNotFoundError(SodRuleConditionError):
    """Raised when a SodRuleCondition with the given id is not found."""

    def __init__(self, condition_id: int) -> None:
        self.condition_id = condition_id
        super().__init__(f'SodRuleCondition {condition_id} not found')


class SodRuleConditionCapabilityNotFoundError(SodRuleConditionError):
    """Raised when one or more capability_ids in the create payload do not exist."""

    def __init__(self, missing_ids: list[int]) -> None:
        self.missing_ids = missing_ids
        super().__init__(f'Capabilities not found: {missing_ids}')


class SodRuleConditionEmptyCapabilitiesError(SodRuleConditionError):
    """Raised when capability_ids is empty."""

    def __init__(self) -> None:
        super().__init__('capability_ids must not be empty')
