# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Feedback domain exceptions."""

from __future__ import annotations


class FeedbackError(Exception):
    """Base class for all Feedback slice errors."""


class FeedbackNotFoundError(FeedbackError):
    """Raised when a Feedback with the given id does not exist."""

    def __init__(self, feedback_id: int) -> None:
        self.feedback_id = feedback_id
        super().__init__(f'Feedback {feedback_id} not found')


class FeedbackTargetMissingError(FeedbackError):
    """Raised when none of rule_id / capability_mapping_id / finding_id are set."""

    def __init__(self) -> None:
        super().__init__('At least one of rule_id, capability_mapping_id, or finding_id must be set')


class FeedbackRuleNotFoundError(FeedbackError):
    """Raised when the referenced SodRule does not exist."""

    def __init__(self, rule_id: int) -> None:
        self.rule_id = rule_id
        super().__init__(f'SodRule {rule_id} not found')


class FeedbackCapabilityMappingNotFoundError(FeedbackError):
    """Raised when the referenced CapabilityMapping does not exist."""

    def __init__(self, capability_mapping_id: int) -> None:
        self.capability_mapping_id = capability_mapping_id
        super().__init__(f'CapabilityMapping {capability_mapping_id} not found')


class FeedbackFindingNotFoundError(FeedbackError):
    """Raised when the referenced Finding does not exist."""

    def __init__(self, finding_id: int) -> None:
        self.finding_id = finding_id
        super().__init__(f'Finding {finding_id} not found')


class FeedbackSubjectNotFoundError(FeedbackError):
    """Raised when the referenced Subject does not exist."""

    def __init__(self, subject_id: object) -> None:
        self.subject_id = subject_id
        super().__init__(f'Subject {subject_id} not found')
