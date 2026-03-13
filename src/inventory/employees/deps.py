# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Employee route dependencies."""

from src.inventory.employees.service import EmployeeService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_employee_service() -> EmployeeService:
    """Return EmployeeService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return EmployeeService(log_service=log_service)
