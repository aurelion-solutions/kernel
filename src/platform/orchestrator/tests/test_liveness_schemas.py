# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ExecutorHeartbeatPayload schema validation."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError
import pytest
from src.platform.orchestrator.liveness_schemas import ExecutorHeartbeatPayload

_NOW_UTC = datetime.now(UTC)


class TestExecutorHeartbeatPayloadValid:
    def test_round_trip(self) -> None:
        """Valid payload round-trips through model_validate."""
        data = {
            'worker_id': 'host-123-0',
            'slot_index': 0,
            'started_at': _NOW_UTC,
            'pipelines_loaded': 5,
        }
        p = ExecutorHeartbeatPayload.model_validate(data)
        assert p.worker_id == 'host-123-0'
        assert p.slot_index == 0
        assert p.pipelines_loaded == 5
        assert p.started_at.tzinfo is not None

    def test_json_round_trip(self) -> None:
        """model_dump(mode='json') produces serialisable dict."""
        p = ExecutorHeartbeatPayload(
            worker_id='h-1-0',
            slot_index=0,
            started_at=_NOW_UTC,
            pipelines_loaded=2,
        )
        dumped = p.model_dump(mode='json')
        assert isinstance(dumped['started_at'], str)
        p2 = ExecutorHeartbeatPayload.model_validate({**dumped, 'started_at': _NOW_UTC})
        assert p2.worker_id == p.worker_id


class TestExecutorHeartbeatPayloadInvalid:
    def test_empty_worker_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match='worker_id'):
            ExecutorHeartbeatPayload(
                worker_id='',
                slot_index=0,
                started_at=_NOW_UTC,
                pipelines_loaded=0,
            )

    def test_negative_slot_index_rejected(self) -> None:
        with pytest.raises(ValidationError, match='slot_index'):
            ExecutorHeartbeatPayload(
                worker_id='h-1-0',
                slot_index=-1,
                started_at=_NOW_UTC,
                pipelines_loaded=0,
            )

    def test_negative_pipelines_loaded_rejected(self) -> None:
        with pytest.raises(ValidationError, match='pipelines_loaded'):
            ExecutorHeartbeatPayload(
                worker_id='h-1-0',
                slot_index=0,
                started_at=_NOW_UTC,
                pipelines_loaded=-1,
            )

    def test_naive_started_at_rejected(self) -> None:
        naive = datetime(2026, 1, 1, 0, 0, 0)  # no tzinfo
        with pytest.raises(ValidationError, match='started_at'):
            ExecutorHeartbeatPayload(
                worker_id='h-1-0',
                slot_index=0,
                started_at=naive,
                pipelines_loaded=0,
            )

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutorHeartbeatPayload(
                worker_id='h-1-0',
                slot_index=0,
                started_at=_NOW_UTC,
                pipelines_loaded=0,
                extra_field='oops',  # type: ignore[call-arg]
            )
