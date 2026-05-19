# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Phase 20 K-J: smoke test for the four default Journey cartridges.

Confirms:
- All four YAML files load without schema / template / action-ref errors.
- Pipeline names match the expected `journey.*` namespace.
- Step graphs are non-empty and reference real registered actions.

The action-ref validator pass needs every engine that the cartridges
reference to have been imported. We do that explicitly at the top of
the module — no autouse fixture in this file because we only run once.
"""

from __future__ import annotations

from pathlib import Path

import src.engines.access_apply.actions  # noqa: F401 — register access_apply.execute_plan
import src.engines.access_plan.actions  # noqa: F401 — register access_plan.plan
import src.engines.notifications.actions  # noqa: F401 — register notifications.send_*
from src.platform.orchestrator.loader import PipelineDefinitionLoader

_JOURNEY_DIR = Path(__file__).resolve().parents[5] / 'cartridges' / 'journey'


def test_journey_cartridges_dir_exists() -> None:
    """The cartridges/journey directory must exist in the monorepo root."""
    assert _JOURNEY_DIR.is_dir(), f'expected directory at {_JOURNEY_DIR}'


def test_all_four_default_cartridges_load() -> None:
    """Every default Journey cartridge loads cleanly with action-ref validation."""
    loader = PipelineDefinitionLoader(validate_action_refs=True)
    result = loader.load_dir(_JOURNEY_DIR)

    expected = {
        'journey.joiner',
        'journey.leaver',
        'journey.on_leave',
        'journey.return_from_leave',
    }
    assert expected.issubset(set(result.keys())), f'missing cartridges: {expected - set(result.keys())}'


def test_journey_leaver_has_wait_for_event() -> None:
    """The leaver cartridge must park on journey.case.apply_confirmed."""
    loader = PipelineDefinitionLoader(validate_action_refs=True)
    result = loader.load_dir(_JOURNEY_DIR)

    leaver = result['journey.leaver']
    wait_steps = [s for s in leaver.steps if s.get('type') == 'wait_for_event']
    assert len(wait_steps) == 1
    assert wait_steps[0]['event'] == 'journey.case.apply_confirmed'


def test_journey_joiner_is_non_destructive() -> None:
    """The joiner cartridge must not have a wait_for_event gate."""
    loader = PipelineDefinitionLoader(validate_action_refs=True)
    result = loader.load_dir(_JOURNEY_DIR)

    joiner = result['journey.joiner']
    wait_steps = [s for s in joiner.steps if s.get('type') == 'wait_for_event']
    assert wait_steps == []


def test_journey_cartridges_step_count_sanity() -> None:
    loader = PipelineDefinitionLoader(validate_action_refs=True)
    result = loader.load_dir(_JOURNEY_DIR)

    # Step counts encoded into the test on purpose — they are part of the
    # cartridge "contract". Any change to the cartridge structure should
    # surface as a test failure that prompts a review of the new template.
    assert len(result['journey.joiner'].steps) == 3
    assert len(result['journey.leaver'].steps) == 5
    assert len(result['journey.on_leave'].steps) == 5
    assert len(result['journey.return_from_leave'].steps) == 2
