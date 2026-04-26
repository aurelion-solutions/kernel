# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for new_root_log_event ContextVar fallback behavior."""

from __future__ import annotations

from src.core.context import correlation_id_var
from src.platform.logs.schemas import LogLevel, LogParticipantKind, new_root_log_event

_COMMON = dict(
    level=LogLevel.INFO,
    message='test message',
    component='test.component',
    initiator_type=LogParticipantKind.SYSTEM,
    initiator_id='system',
    actor_type=LogParticipantKind.SYSTEM,
    actor_id='system',
    target_type=LogParticipantKind.SYSTEM,
    target_id='target',
)


def test_new_root_log_event_falls_back_to_contextvar() -> None:
    token = correlation_id_var.set('ctx-log-cid')
    try:
        event = new_root_log_event(correlation_id=None, **_COMMON)  # type: ignore[arg-type]
    finally:
        correlation_id_var.reset(token)

    assert event.correlation_id == 'ctx-log-cid'


def test_new_root_log_event_explicit_wins_over_contextvar() -> None:
    token = correlation_id_var.set('ctx-log-cid')
    try:
        event = new_root_log_event(correlation_id='explicit-cid', **_COMMON)  # type: ignore[arg-type]
    finally:
        correlation_id_var.reset(token)

    assert event.correlation_id == 'explicit-cid'
