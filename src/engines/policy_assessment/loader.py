# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""YAML loader for PDP policy rules.

Reads lifecycle.yaml, risk.yaml, and mapping.yaml from a directory and
returns a validated RulePack.  Pure I/O — no side effects.
"""

from pathlib import Path
from typing import Any

from pydantic import ValidationError
import yaml
from src.engines.policy_assessment.schemas import Rule, RulePack

_VALID_KINDS = frozenset({'lifecycle', 'risk'})
_REQUIRED_RULE_FIELDS = frozenset({'id', 'kind', 'when', 'then', 'precedence'})


class PolicyLoadError(Exception):
    """Raised when YAML policy files are missing, malformed, or invalid."""


def _read_yaml(path: Path) -> Any:
    try:
        with path.open('rb') as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise PolicyLoadError(f'Policy file not found: {path}') from exc
    except yaml.YAMLError as exc:
        raise PolicyLoadError(f'Failed to parse YAML file {path}: {exc}') from exc


def _parse_rules_file(path: Path) -> list[dict[str, Any]]:
    data = _read_yaml(path)
    if not isinstance(data, dict) or 'rules' not in data:
        raise PolicyLoadError(f'{path.name} must have a top-level "rules" key')
    rules_raw = data['rules']
    if not isinstance(rules_raw, list):
        raise PolicyLoadError(f'{path.name}: "rules" must be a list')
    return rules_raw  # type: ignore[return-value]


def _validate_rule(raw: dict[str, Any], source_file: str) -> Rule:
    missing = _REQUIRED_RULE_FIELDS - raw.keys()
    if missing:
        raise PolicyLoadError(f'{source_file}: rule is missing required fields {sorted(missing)}: {raw}')

    rule_id = raw.get('id', '<unknown>')
    kind = raw.get('kind', '')
    if kind not in _VALID_KINDS:
        raise PolicyLoadError(
            f'{source_file}: rule "{rule_id}" has invalid kind "{kind}"; must be one of {sorted(_VALID_KINDS)}'
        )

    try:
        return Rule.model_validate(raw)
    except ValidationError as exc:
        raise PolicyLoadError(f'{source_file}: invalid rule "{rule_id}": {exc}') from exc


def _parse_mapping_file(path: Path) -> dict[str, dict[str, Any]]:
    data = _read_yaml(path)
    if not isinstance(data, list):
        raise PolicyLoadError(f'{path.name} must contain a list of objects')
    result: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise PolicyLoadError(f'{path.name}: each mapping entry must be an object')
        if 'application' not in item or 'map' not in item:
            raise PolicyLoadError(f'{path.name}: each mapping entry must have "application" and "map" keys')
        app = item['application']
        mapping_map = item['map']
        if not isinstance(app, str) or not app:
            raise PolicyLoadError(f'{path.name}: "application" must be a non-empty string')
        if not isinstance(mapping_map, dict):
            raise PolicyLoadError(f'{path.name}: "map" for application "{app}" must be a dict')
        result[app] = mapping_map
    return result


def load_rules_from_yaml(policies_dir: Path) -> RulePack:
    """Load and validate rules from lifecycle.yaml, risk.yaml, and mapping.yaml.

    Args:
        policies_dir: Directory containing the three YAML files.

    Returns:
        A fully populated RulePack.

    Raises:
        PolicyLoadError: on any missing file, parse error, or validation failure.
    """
    lifecycle_path = policies_dir / 'lifecycle.yaml'
    risk_path = policies_dir / 'risk.yaml'
    mapping_path = policies_dir / 'mapping.yaml'

    # Parse raw rule dicts
    lifecycle_raw = _parse_rules_file(lifecycle_path)
    risk_raw = _parse_rules_file(risk_path)

    # Validate and construct Rule objects; collect IDs for uniqueness check
    seen_ids: set[str] = set()
    lifecycle_rules: list[Rule] = []
    risk_rules: list[Rule] = []

    for raw in lifecycle_raw:
        rule = _validate_rule(raw, 'lifecycle.yaml')
        if rule.id in seen_ids:
            raise PolicyLoadError(f'Duplicate rule id "{rule.id}" in lifecycle.yaml')
        seen_ids.add(rule.id)
        lifecycle_rules.append(rule)

    for raw in risk_raw:
        rule = _validate_rule(raw, 'risk.yaml')
        if rule.id in seen_ids:
            raise PolicyLoadError(f'Duplicate rule id "{rule.id}" across lifecycle.yaml and risk.yaml')
        seen_ids.add(rule.id)
        risk_rules.append(rule)

    mapping = _parse_mapping_file(mapping_path)

    return RulePack(lifecycle=lifecycle_rules, risk=risk_rules, mapping=mapping)
