# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Subject route dependencies."""

from src.inventory.subjects.service import SubjectService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_subject_service() -> SubjectService:
    """Return SubjectService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return SubjectService(log_service=log_service)
