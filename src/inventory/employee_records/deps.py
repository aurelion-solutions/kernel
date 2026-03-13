# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EmployeeRecord route dependencies."""

from src.inventory.employee_records.service import EmployeeRecordService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_employee_record_service() -> EmployeeRecordService:
    """Return EmployeeRecordService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return EmployeeRecordService(log_service=log_service)
