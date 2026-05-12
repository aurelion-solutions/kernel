# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Well-known pipeline schema merge and action catalogue helpers (Step 11).

Design notes
------------
- ``build_merged_pipeline_schema`` loads the bundled ``schema.json`` once into a
  module-level cache keyed by file mtime (cheap reload-friendly). Returns a
  deep copy with per-action arg / result schemas injected under ``$defs``.
  The merge is ADDITIVE — no existing $defs entries are overwritten and no
  ``step_engine_call.args`` constraints are tightened (Step 14 territory).
- ``build_action_catalogue`` is a direct mapping over ``ACTION_REGISTRY.all()``.
- No logging.getLogger / print / structlog — LogService is injected by the caller.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_component_trace_fields
from src.platform.orchestrator.loader import PipelineDefinitionLoader
from src.platform.orchestrator.registry import RegisteredAction
from src.platform.orchestrator.schemas import ActionDescriptor

# ---------------------------------------------------------------------------
# Module-level schema cache: (path, mtime) → dict
# ---------------------------------------------------------------------------

_SCHEMA_CACHE: dict[tuple[str, float], dict[str, Any]] = {}

_COMPONENT = 'orchestrator_discovery'


def _load_base_schema(path: Path, log_service: LogService | NoOpLogService) -> dict[str, Any]:
    """Return the base ``schema.json`` contents, using a mtime-keyed cache.

    Cache miss is logged at DEBUG level.
    # allowed-emit-safe: observability
    """
    mtime = path.stat().st_mtime
    cache_key = (str(path), mtime)
    if cache_key in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[cache_key]

    log_service.emit_safe(
        level=LogLevel.DEBUG,
        message='Pipeline schema cache miss — loading from disk',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {'schema_path': str(path)},
            component_id=_COMPONENT,
            target_id='pipeline_schema',
        ),
    )

    with path.open('rb') as fh:
        schema: dict[str, Any] = json.load(fh)

    # Evict old entries for this path (keep only current mtime).
    stale_keys = [k for k in _SCHEMA_CACHE if k[0] == str(path)]
    for k in stale_keys:
        del _SCHEMA_CACHE[k]

    _SCHEMA_CACHE[cache_key] = schema
    return schema


def build_merged_pipeline_schema(
    actions: list[RegisteredAction],
    *,
    log_service: LogService | NoOpLogService | None = None,
    schema_path: Path | None = None,
) -> dict[str, Any]:
    """Return a deep copy of the bundled pipeline schema with action schemas injected.

    Injection points (both ADDITIVE — no existing entries overwritten):
    - ``$defs.action_args``   keyed by ``<engine>.<action>`` → args_schema JSON Schema.
    - ``$defs.action_results`` keyed by ``<engine>.<action>`` → result_schema JSON Schema.

    NOTE: Step 14 territory — tightening ``step_engine_call.args`` requires a
    discriminator over (engine, action) pairs and is explicitly out of scope here.
    External consumers must use ``$defs.action_args.<engine>.<action>`` directly.

    Multi-version coexistence is out of scope for this phase (single-version-per-name
    invariant). Step 14 (hot reload) decides key strategy.
    """
    _log = log_service if log_service is not None else NoOpLogService()
    _path: Path = schema_path if schema_path is not None else PipelineDefinitionLoader._DEFAULT_SCHEMA_PATH

    base = _load_base_schema(_path, _log)
    merged: dict[str, Any] = copy.deepcopy(base)

    defs: dict[str, Any] = merged.setdefault('$defs', {})
    action_args: dict[str, Any] = defs.setdefault('action_args', {})
    action_results: dict[str, Any] = defs.setdefault('action_results', {})

    for entry in actions:
        key = f'{entry.engine}.{entry.action}'
        action_args[key] = entry.args_schema.model_json_schema()
        action_results[key] = entry.result_schema.model_json_schema()

    return merged


def build_action_catalogue(actions: list[RegisteredAction]) -> list[ActionDescriptor]:
    """Return the full action catalogue as a list of ActionDescriptor instances."""
    return [
        ActionDescriptor(
            engine=entry.engine,
            action=entry.action,
            idempotent=entry.idempotent,
            args_schema=entry.args_schema.model_json_schema(),
            result_schema=entry.result_schema.model_json_schema(),
        )
        for entry in actions
    ]
