# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from src.platform.connectors.models import ConnectorInstance


def list_connector_instances_matching_tags(
    instances: list[ConnectorInstance],
    required_tags: list[str],
) -> list[ConnectorInstance]:
    """Instances whose tag set is a superset of ``required_tags`` (same rule as runtime selection).

    If ``required_tags`` is empty, every instance in the input list matches.
    """
    required = set(required_tags or [])
    matched: list[ConnectorInstance] = []
    for instance in instances:
        instance_tags = set(instance.tags or [])
        if required.issubset(instance_tags):
            matched.append(instance)
    return matched


def select_connector_instance_by_tags(
    instances: list[ConnectorInstance],
    required_tags: list[str],
) -> ConnectorInstance | None:
    matched = list_connector_instances_matching_tags(instances, required_tags)
    return matched[0] if matched else None
