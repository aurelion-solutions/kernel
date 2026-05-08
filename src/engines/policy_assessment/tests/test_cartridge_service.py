# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PolicyCartridgeAssessmentService."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src.engines.policy_assessment.cartridge_service import PolicyCartridgeAssessmentService
from src.engines.policy_assessment.contracts import PolicyAssessmentOutput, PolicyAssessmentRequest
from src.engines.policy_assessment.dispatcher import PolicyAssessmentDispatcher
from src.engines.policy_assessment.schemas import AbstractState, Decision
from src.inventory.policy.cartridges.loader import CartridgeLoadError, FileCartridgeLoader
from src.inventory.policy.enums import AssessmentStrategy, PolicyType

LENS_CARTRIDGES_DIR = Path(__file__).parent.parent.parent.parent.parent.parent / 'cartridges' / 'lens'

_ORPHANED_ACCESS_PATH = LENS_CARTRIDGES_DIR / 'access_risk' / 'orphaned_access.yaml'

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_CONTEXT = {
    'subject': {'id': 's-1', 'kind': 'employee', 'status': 'active'},
    'now': _NOW.isoformat(),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_dispatcher(abstract_state: AbstractState = AbstractState.enabled) -> PolicyAssessmentDispatcher:
    decision = Decision(abstract_state=abstract_state)
    output = PolicyAssessmentOutput(matched=abstract_state == AbstractState.enabled, decision=decision)
    dispatcher = MagicMock(spec=PolicyAssessmentDispatcher)
    dispatcher.evaluate.return_value = output
    return dispatcher


def _mock_loader(path: Path) -> FileCartridgeLoader:
    real_loader = FileCartridgeLoader()
    manifest = real_loader.load_file(path)
    loader = MagicMock(spec=FileCartridgeLoader)
    loader.load_file.return_value = manifest
    return loader


# ---------------------------------------------------------------------------
# Flow verification with mocks
# ---------------------------------------------------------------------------


def test_evaluate_file_returns_policy_assessment_output() -> None:
    svc = PolicyCartridgeAssessmentService(
        loader=_mock_loader(_ORPHANED_ACCESS_PATH),
        dispatcher=_mock_dispatcher(),
    )
    result = svc.evaluate_file(_ORPHANED_ACCESS_PATH, _CONTEXT)
    assert isinstance(result, PolicyAssessmentOutput)


def test_evaluate_file_calls_loader_with_path() -> None:
    loader = _mock_loader(_ORPHANED_ACCESS_PATH)
    svc = PolicyCartridgeAssessmentService(loader=loader, dispatcher=_mock_dispatcher())
    svc.evaluate_file(_ORPHANED_ACCESS_PATH, _CONTEXT)
    loader.load_file.assert_called_once_with(_ORPHANED_ACCESS_PATH)


def test_evaluate_file_calls_dispatcher_evaluate() -> None:
    dispatcher = _mock_dispatcher()
    svc = PolicyCartridgeAssessmentService(
        loader=_mock_loader(_ORPHANED_ACCESS_PATH),
        dispatcher=dispatcher,
    )
    svc.evaluate_file(_ORPHANED_ACCESS_PATH, _CONTEXT)
    dispatcher.evaluate.assert_called_once()


def test_evaluate_file_passes_request_to_dispatcher() -> None:
    dispatcher = _mock_dispatcher()
    svc = PolicyCartridgeAssessmentService(
        loader=_mock_loader(_ORPHANED_ACCESS_PATH),
        dispatcher=dispatcher,
    )
    svc.evaluate_file(_ORPHANED_ACCESS_PATH, _CONTEXT)

    call_args = dispatcher.evaluate.call_args
    request: PolicyAssessmentRequest = call_args[0][0]
    assert isinstance(request, PolicyAssessmentRequest)
    assert request.policy_type == PolicyType.ACCESS_RISK
    assert request.assessment_strategy == AssessmentStrategy.DETERMINISTIC
    assert request.policy_id == 'lens.access_risk.orphaned_access'
    assert request.context == _CONTEXT


def test_evaluate_file_matched_true_when_enabled() -> None:
    svc = PolicyCartridgeAssessmentService(
        loader=_mock_loader(_ORPHANED_ACCESS_PATH),
        dispatcher=_mock_dispatcher(AbstractState.enabled),
    )
    result = svc.evaluate_file(_ORPHANED_ACCESS_PATH, _CONTEXT)
    assert result.matched is True


def test_evaluate_file_matched_false_when_disabled() -> None:
    svc = PolicyCartridgeAssessmentService(
        loader=_mock_loader(_ORPHANED_ACCESS_PATH),
        dispatcher=_mock_dispatcher(AbstractState.disabled),
    )
    result = svc.evaluate_file(_ORPHANED_ACCESS_PATH, _CONTEXT)
    assert result.matched is False


# ---------------------------------------------------------------------------
# Real cartridge reaches real dispatcher
# ---------------------------------------------------------------------------


def test_real_cartridge_reaches_dispatcher() -> None:
    real_dispatcher = MagicMock(spec=PolicyAssessmentDispatcher)
    real_dispatcher.evaluate.return_value = PolicyAssessmentOutput(matched=True)

    svc = PolicyCartridgeAssessmentService(dispatcher=real_dispatcher)
    svc.evaluate_file(_ORPHANED_ACCESS_PATH, _CONTEXT)

    real_dispatcher.evaluate.assert_called_once()
    request: PolicyAssessmentRequest = real_dispatcher.evaluate.call_args[0][0]
    assert request.policy_id == 'lens.access_risk.orphaned_access'
    assert request.policy_type == PolicyType.ACCESS_RISK


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


def test_loader_error_propagates() -> None:
    loader = MagicMock(spec=FileCartridgeLoader)
    loader.load_file.side_effect = CartridgeLoadError('file not found')
    svc = PolicyCartridgeAssessmentService(loader=loader, dispatcher=_mock_dispatcher())

    with pytest.raises(CartridgeLoadError):
        svc.evaluate_file(Path('missing.yaml'), _CONTEXT)


def test_unsupported_strategy_propagates_not_implemented() -> None:
    dispatcher = MagicMock(spec=PolicyAssessmentDispatcher)
    dispatcher.evaluate.side_effect = NotImplementedError('not yet wired')

    real_loader = FileCartridgeLoader()
    manifest = real_loader.load_file(_ORPHANED_ACCESS_PATH)
    loader = MagicMock(spec=FileCartridgeLoader)
    loader.load_file.return_value = manifest

    svc = PolicyCartridgeAssessmentService(loader=loader, dispatcher=dispatcher)

    with pytest.raises(NotImplementedError, match='not yet wired'):
        svc.evaluate_file(_ORPHANED_ACCESS_PATH, _CONTEXT)
