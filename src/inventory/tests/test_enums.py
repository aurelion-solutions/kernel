# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from src.inventory.enums import Action


def test_action_has_exactly_five_members() -> None:
    assert len(Action) == 5


def test_action_values_match_names() -> None:
    for member in Action:
        assert member.value == member.name


def test_action_is_str() -> None:
    assert isinstance(Action.read, str)


def test_action_members_are_correct_set() -> None:
    assert {m.value for m in Action} == {
        'read',
        'write',
        'execute',
        'approve',
        'administer',
    }
