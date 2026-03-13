# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Customer route dependencies."""

from src.inventory.customers.service import CustomerService
from src.inventory.subjects.service import SubjectService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_customer_service() -> CustomerService:
    """Return CustomerService with injected log service and subject service."""
    log_service = LogService(factory=log_sink_factory)
    subject_service = SubjectService(log_service=log_service)
    return CustomerService(log_service=log_service, subject_service=subject_service)
