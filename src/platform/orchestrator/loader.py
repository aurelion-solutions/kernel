# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pipeline definition loader for the native orchestrator.

Public surface
--------------
- :class:`PipelineDefinition`  — frozen dataclass; one instance per loaded YAML.
- :class:`PipelineDefinitionLoader`  — one-shot, fail-fast loader.

Custom exceptions (all inherit :class:`PipelineLoadError`)
----------------------------------------------------------
- :class:`PipelineSchemaError`       — JSON Schema structural validation failure.
- :class:`PipelineActionRefError`    — step references an unregistered (engine, action).
- :class:`PipelineRequiresOrderError`— ``requires`` list contains forward or unknown refs.
- :class:`PipelineTemplatingError`   — ``${...}`` template ref is invalid.
- :class:`PipelineTriggerError`      — trigger declaration violates semantic rules.

Design invariants
-----------------
- No logging — caller (Step 7 service / Step 12 runner) decides how to surface errors.
- No I/O at construction; JSON Schema validator compiled lazily on first call.
- ``load_dir`` returns an empty dict for missing/empty directories (never raises for those).
- Duplicate pipeline name across files → :class:`PipelineLoadError`.
- ``schema.json`` is excluded from ``*.yaml`` glob (it is not a YAML file anyway).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import yaml
from src.platform.orchestrator.registry import ACTION_REGISTRY, ActionNotFoundError

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class PipelineLoadError(Exception):
    """Base exception for all pipeline loader errors."""


class PipelineSchemaError(PipelineLoadError):
    """JSON Schema structural validation failed."""


class PipelineActionRefError(PipelineLoadError):
    """A step references a (engine, action) pair not in ACTION_REGISTRY."""


class PipelineRequiresOrderError(PipelineLoadError):
    """A ``requires`` entry is a forward reference, self-reference, or unknown step."""


class PipelineTemplatingError(PipelineLoadError):
    """A ``${...}`` template expression references an undeclared arg or step."""


class PipelineTriggerError(PipelineLoadError):
    """Trigger declaration violates semantic rules (duplicate schedule, bad args)."""


# ---------------------------------------------------------------------------
# PipelineDefinition
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipelineDefinition:
    """Immutable, fully-validated in-memory representation of one pipeline YAML.

    Constructed exclusively by :class:`PipelineDefinitionLoader`; never build
    directly in application code.
    """

    name: str
    version: int
    schema_version: int
    source_path: Path
    # sha256(canonical_json(raw_dict)) — stable across key ordering and whitespace.
    content_hash: str  # NOTE: default=str in canonical_json handles non-primitive values
    args_schema_dict: dict[str, Any]
    triggers: tuple[Mapping[str, Any], ...]
    steps: tuple[Mapping[str, Any], ...]
    raw_dict: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Templating helper
# ---------------------------------------------------------------------------

# Matches ${args.X} and ${steps.<sname>.result.<path>}
# ${...} is ALWAYS treated as a live reference — $$ escape is NOT supported.
_TEMPLATE_RE = re.compile(r'\$\{(args\.[a-zA-Z0-9_]+|steps\.[a-z][a-z0-9_]*\.result\.[a-zA-Z0-9_.]+)\}')


def _transitive_requires(step_name: str, requires_graph: dict[str, list[str]]) -> set[str]:
    """BFS over the requires graph; returns all transitive ancestors of *step_name*."""
    visited: set[str] = set()
    queue: deque[str] = deque(requires_graph.get(step_name, []))
    while queue:
        node = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        queue.extend(requires_graph.get(node, []))
    return visited


# ---------------------------------------------------------------------------
# PipelineDefinitionLoader
# ---------------------------------------------------------------------------


class PipelineDefinitionLoader:
    """Validates and loads pipeline YAML definitions.

    Parameters
    ----------
    schema_path:
        Path to ``schema.json``.  Defaults to
        ``<repo-root>/pipelines/schema.json`` resolved relative to this
        module's location (three parents up, then ``pipelines/schema.json``).
        No I/O at construction — validator is compiled lazily.
    """

    _DEFAULT_SCHEMA_PATH = Path(__file__).parent.parent.parent.parent / 'pipelines' / 'schema.json'

    def __init__(self, schema_path: Path | None = None) -> None:
        self._schema_path: Path = schema_path if schema_path is not None else self._DEFAULT_SCHEMA_PATH
        self._validator: Any = None  # Draft202012Validator, built lazily

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_file(self, path: Path) -> PipelineDefinition:
        """Load and validate a single pipeline YAML.

        Raises
        ------
        PipelineLoadError
            File not found, invalid YAML, or root is not a mapping.
        PipelineSchemaError
            Structural JSON Schema violation.
        PipelineActionRefError
            Step references an unregistered action.
        PipelineRequiresOrderError
            ``requires`` entry is a forward/unknown/self reference.
        PipelineTemplatingError
            ``${...}`` references an undeclared pipeline arg or step.
        PipelineTriggerError
            Duplicate schedule trigger or schedule args mismatch.
        """
        raw = self._read_yaml(path)
        self._validate_schema(raw, path)
        pipeline = raw['pipeline']
        self._check_action_refs(pipeline, path)
        requires_graph = self._check_requires_order(pipeline, path)
        self._check_templating(pipeline, requires_graph, path)
        self._check_triggers(pipeline, path)
        return self._build(raw, pipeline, path)

    def load_dir(self, path: Path) -> dict[str, PipelineDefinition]:
        """Load all ``*.yaml`` files from *path*.

        - Missing directory → ``{}``.
        - Empty directory → ``{}``.
        - ``schema.json`` is excluded (it is not a YAML file).
        - Files processed in sorted order for deterministic results.
        - Duplicate ``pipeline.name`` across files → :class:`PipelineLoadError`.
        """
        if not path.exists() or not path.is_dir():
            return {}

        result: dict[str, PipelineDefinition] = {}
        seen_paths: dict[str, Path] = {}

        for yaml_path in sorted(path.glob('*.yaml')):
            defn = self.load_file(yaml_path)
            if defn.name in seen_paths:
                raise PipelineLoadError(
                    f'Duplicate pipeline name {defn.name!r}: found in {seen_paths[defn.name].name} and {yaml_path.name}'
                )
            seen_paths[defn.name] = yaml_path
            result[defn.name] = defn

        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_validator(self) -> Any:
        """Return the cached Draft202012Validator, building it on first call."""
        if self._validator is None:
            from jsonschema import Draft202012Validator

            with self._schema_path.open('rb') as fh:
                schema = json.load(fh)
            self._validator = Draft202012Validator(schema)
        return self._validator

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        """Read and parse *path* as YAML; raise :class:`PipelineLoadError` on failure."""
        try:
            with path.open('rb') as fh:
                raw = yaml.safe_load(fh)
        except FileNotFoundError as exc:
            raise PipelineLoadError(f'{path.name}: file not found') from exc
        except yaml.YAMLError as exc:
            raise PipelineLoadError(f'{path.name}: invalid YAML — {exc}') from exc

        if not isinstance(raw, dict):
            raise PipelineLoadError(f'{path.name}: pipeline YAML root must be a mapping, got {type(raw).__name__}')
        return raw

    def _validate_schema(self, raw: dict[str, Any], path: Path) -> None:
        """Run JSON Schema validation; raise :class:`PipelineSchemaError` on first error."""
        validator = self._get_validator()
        errors = list(validator.iter_errors(raw))
        if errors:
            first = errors[0]
            raise PipelineSchemaError(f'{path.name}: {first.message}')

    def _check_action_refs(self, pipeline: dict[str, Any], path: Path) -> None:
        """Verify every engine-call step references a registered action."""
        for step in pipeline.get('steps', []):
            if 'engine' in step and 'action' in step:
                try:
                    ACTION_REGISTRY.get(step['engine'], step['action'])
                except ActionNotFoundError as exc:
                    raise PipelineActionRefError(f'{path.name}: step {step["name"]!r} — {exc}') from exc

    def _check_requires_order(
        self,
        pipeline: dict[str, Any],
        path: Path,
    ) -> dict[str, list[str]]:
        """Verify ``requires`` only references already-seen steps.

        Returns the ``requires`` graph (step_name → [dep_name, ...]) for
        subsequent transitive-closure checks in :meth:`_check_templating`.
        """
        seen: set[str] = set()
        requires_graph: dict[str, list[str]] = {}

        for step in pipeline.get('steps', []):
            step_name: str = step['name']
            deps: list[str] = step.get('requires', [])
            for dep in deps:
                if dep not in seen:
                    raise PipelineRequiresOrderError(
                        f'{path.name}: step {step_name!r} requires {dep!r} '
                        f'which is not yet defined (forward or unknown reference)'
                    )
            requires_graph[step_name] = deps
            seen.add(step_name)

        return requires_graph

    def _check_templating(
        self,
        pipeline: dict[str, Any],
        requires_graph: dict[str, list[str]],
        path: Path,
    ) -> None:
        """Validate all ``${...}`` template expressions within step args.

        - ``${args.X}`` → X must be in ``pipeline.args.properties``.
        - ``${steps.<sname>.result.<...>}`` → sname must be in the transitive
          ``requires`` closure of the current step.
        """
        args_props: set[str] = set(pipeline.get('args', {}).get('properties', {}).keys())

        for step in pipeline.get('steps', []):
            step_name: str = step['name']
            step_args = step.get('args')
            if not step_args:
                continue

            args_json = json.dumps(step_args)
            transitive = _transitive_requires(step_name, requires_graph)

            for match in _TEMPLATE_RE.finditer(args_json):
                ref = match.group(1)
                if ref.startswith('args.'):
                    arg_key = ref[len('args.') :]
                    if arg_key not in args_props:
                        raise PipelineTemplatingError(
                            f'{path.name}: step {step_name!r} references '
                            f'${{args.{arg_key}}} but {arg_key!r} is not declared '
                            f'in pipeline.args.properties'
                        )
                else:
                    # steps.<sname>.result.<...>
                    parts = ref.split('.')
                    ref_step = parts[1]
                    if ref_step not in transitive:
                        raise PipelineTemplatingError(
                            f'{path.name}: step {step_name!r} references '
                            f'${{steps.{ref_step}.result...}} but {ref_step!r} is not '
                            f'in the transitive requires closure of {step_name!r}'
                        )

    # Regex for valid pipeline arg names (mirrors pipeline YAML schema).
    _ARG_NAME_RE = re.compile(r'^[a-z][a-z0-9_]*$')

    def _check_triggers(self, pipeline: dict[str, Any], path: Path) -> None:
        """Validate trigger-level semantic rules.

        - At most one ``schedule`` trigger.
        - Schedule ``args`` keys must be a subset of ``pipeline.args.properties``,
          and values must be valid per the pipeline args JSON Schema.
        - MQ triggers: ``args_from_payload`` must be a mapping of valid arg names
          to dotted-path strings.
        """
        triggers = pipeline.get('triggers', [])
        schedule_triggers = [t for t in triggers if t.get('type') == 'schedule']
        mq_triggers = [t for t in triggers if t.get('type') == 'mq']

        if len(schedule_triggers) > 1:
            raise PipelineTriggerError(
                f'{path.name}: at most one schedule trigger is allowed, found {len(schedule_triggers)}'
            )

        if schedule_triggers:
            schedule = schedule_triggers[0]
            trigger_args = schedule.get('args')
            if trigger_args:
                pipeline_args_schema = pipeline.get('args', {})
                declared_props: set[str] = set(pipeline_args_schema.get('properties', {}).keys())

                for key in trigger_args:
                    if key not in declared_props:
                        raise PipelineTriggerError(
                            f'{path.name}: schedule trigger arg {key!r} is not declared in pipeline.args.properties'
                        )

                if pipeline_args_schema:
                    from jsonschema import Draft202012Validator

                    schema_validator = Draft202012Validator(pipeline_args_schema)
                    errors = list(schema_validator.iter_errors(trigger_args))
                    if errors:
                        raise PipelineTriggerError(
                            f'{path.name}: schedule trigger args fail pipeline args schema — {errors[0].message}'
                        )

        for mq_trigger in mq_triggers:
            self._check_mq_trigger(mq_trigger, path)

    def _check_mq_trigger(self, trigger: dict[str, Any], path: Path) -> None:
        """Validate semantic rules for a single mq trigger.

        - ``args_from_payload`` must be a mapping (if present).
        - Each key must match ``^[a-z][a-z0-9_]*$`` (valid arg name).
        - Each value must be a non-empty string (dotted payload path).
        """
        args_from_payload = trigger.get('args_from_payload')
        if args_from_payload is None:
            return

        if not isinstance(args_from_payload, Mapping):
            raise PipelineTriggerError(
                f'{path.name}: mq trigger args_from_payload must be a mapping, got {type(args_from_payload).__name__}'
            )

        for key, value in args_from_payload.items():
            if not self._ARG_NAME_RE.match(str(key)):
                raise PipelineTriggerError(
                    f'{path.name}: mq trigger args_from_payload key {key!r} is not a valid arg name '
                    f'(must match ^[a-z][a-z0-9_]*$)'
                )
            if not isinstance(value, str) or not value.strip():
                raise PipelineTriggerError(
                    f'{path.name}: mq trigger args_from_payload value for {key!r} must be a non-empty string '
                    f'(dotted payload path), got {value!r}'
                )

    @staticmethod
    def _canonical_json(raw: dict[str, Any]) -> str:
        # default=str handles non-primitive values (e.g. datetime) that may appear
        # in pipeline YAML — ensures the hash never crashes on exotic scalar types.
        return json.dumps(raw, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)

    def _build(
        self,
        raw: dict[str, Any],
        pipeline: dict[str, Any],
        path: Path,
    ) -> PipelineDefinition:
        content_hash = hashlib.sha256(self._canonical_json(raw).encode()).hexdigest()
        return PipelineDefinition(
            name=pipeline['name'],
            version=pipeline['version'],
            schema_version=pipeline['schema_version'],
            source_path=path,
            content_hash=content_hash,
            args_schema_dict=dict(pipeline.get('args', {})),
            triggers=tuple(pipeline.get('triggers', [])),
            steps=tuple(pipeline.get('steps', [])),
            raw_dict=raw,
        )
