# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for connector instance tag selection helpers."""

from src.platform.connectors.models import ConnectorInstance
from src.platform.connectors.selector import (
    list_connector_instances_matching_tags,
    select_connector_instance_by_tags,
)


def _inst(iid: str, tags: list[str]) -> ConnectorInstance:
    return ConnectorInstance(instance_id=iid, tags=tags)


def test_list_connector_instances_matching_tags_subset_rule() -> None:
    instances = [
        _inst('a', ['jira', 'eu']),
        _inst('b', ['jira']),
        _inst('c', ['jira', 'eu', 'prod']),
    ]
    matched = list_connector_instances_matching_tags(instances, ['jira', 'eu'])
    assert [m.instance_id for m in matched] == ['a', 'c']


def test_list_connector_instances_matching_tags_empty_required_matches_all() -> None:
    instances = [_inst('x', ['a']), _inst('y', [])]
    matched = list_connector_instances_matching_tags(instances, [])
    assert [m.instance_id for m in matched] == ['x', 'y']


def test_select_connector_instance_by_tags_returns_first_in_order() -> None:
    instances = [_inst('z1', ['t']), _inst('z2', ['t'])]
    one = select_connector_instance_by_tags(instances, ['t'])
    assert one is not None
    assert one.instance_id == 'z1'
