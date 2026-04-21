# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Platform logs package."""

from src.platform.logs.factory import (
    LogSinkFactory,
    UnsupportedProviderError,
    log_sink_factory,
)
from src.platform.logs.interface import LogSink
from src.platform.logs.service import NoOpLogService, noop_log_service
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.schemas import (
    LogEvent,
    LogLevel,
    LogParticipantKind,
    new_downstream_log_event,
    new_downstream_log_event_from_parent_id,
    new_root_log_event,
)
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

__all__ = [
    'CapturingLogSink',
    'NoOpLogService',
    'FileLogSink',
    'noop_log_service',
    'LogEvent',
    'LogLevel',
    'LogParticipantKind',
    'LogSink',
    'LogSinkFactory',
    'LogService',
    'UnsupportedProviderError',
    'log_sink_factory',
    'new_downstream_log_event',
    'new_downstream_log_event_from_parent_id',
    'new_root_log_event',
]
