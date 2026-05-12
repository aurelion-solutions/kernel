# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PipelineDefinitionLoader and PipelineDefinition."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel
import pytest
from src.platform.orchestrator.loader import (
    PipelineActionRefError,
    PipelineDefinition,
    PipelineDefinitionLoader,
    PipelineLoadError,
    PipelineRequiresOrderError,
    PipelineSchemaError,
    PipelineTemplatingError,
    PipelineTriggerError,
)
from src.platform.orchestrator.registry import ACTION_REGISTRY

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


class _EmptyArgs(BaseModel):
    pass


class _EmptyResult(BaseModel):
    pass


async def _noop(args: BaseModel, ctx: Any) -> dict[str, Any]:
    return {}


def _register(engine: str, action: str) -> None:
    ACTION_REGISTRY.register(engine, action, _EmptyArgs, _EmptyResult, True, _noop)


def _write_yaml(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding='utf-8')
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_registry() -> Any:
    ACTION_REGISTRY._clear_for_tests()
    yield
    ACTION_REGISTRY._clear_for_tests()


@pytest.fixture()
def loader() -> PipelineDefinitionLoader:
    return PipelineDefinitionLoader()


# ---------------------------------------------------------------------------
# Minimal valid YAML template
# ---------------------------------------------------------------------------

_MINIMAL_YAML = """\
pipeline:
  name: my_pipe
  version: 1
  schema_version: 1
  steps:
    - name: step_a
      engine: eng
      action: act
"""

_TRIGGER_MQ_YAML = """\
pipeline:
  name: my_pipe
  version: 1
  schema_version: 1
  args:
    type: object
    properties:
      tenant_id:
        type: string
  triggers:
    - type: mq
      routing_key: identity.created
  steps:
    - name: step_a
      engine: eng
      action: act
    - name: step_b
      engine: eng
      action: act
"""

# ---------------------------------------------------------------------------
# Test 1: happy path — args + 2 engine steps + 1 mq trigger
# ---------------------------------------------------------------------------


def test_happy_path(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    _register('eng', 'act')
    path = _write_yaml(tmp_path, 'pipe.yaml', _TRIGGER_MQ_YAML)
    defn = loader.load_file(path)

    assert isinstance(defn, PipelineDefinition)
    assert defn.name == 'my_pipe'
    assert defn.version == 1
    assert defn.schema_version == 1
    assert defn.source_path == path
    assert len(defn.content_hash) == 64  # sha256 hex
    assert len(defn.steps) == 2
    assert len(defn.triggers) == 1
    assert defn.triggers[0]['type'] == 'mq'
    assert 'tenant_id' in defn.args_schema_dict.get('properties', {})


# ---------------------------------------------------------------------------
# Test 2: load_dir happy multi-file
# ---------------------------------------------------------------------------


def test_load_dir_multi_file(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    _register('eng', 'act')
    _write_yaml(tmp_path, 'alpha.yaml', _MINIMAL_YAML)
    beta = _MINIMAL_YAML.replace('name: my_pipe', 'name: beta_pipe')
    _write_yaml(tmp_path, 'beta.yaml', beta)

    result = loader.load_dir(tmp_path)
    assert set(result.keys()) == {'my_pipe', 'beta_pipe'}
    assert all(isinstance(v, PipelineDefinition) for v in result.values())


# ---------------------------------------------------------------------------
# Test 3: empty directory → {}
# ---------------------------------------------------------------------------


def test_load_dir_empty(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    result = loader.load_dir(tmp_path)
    assert result == {}


# ---------------------------------------------------------------------------
# Test 4: missing directory → {}
# ---------------------------------------------------------------------------


def test_load_dir_missing(loader: PipelineDefinitionLoader) -> None:
    result = loader.load_dir(Path('/no/such/directory/aurelion_test'))
    assert result == {}


# ---------------------------------------------------------------------------
# Test 5: schema.json present in dir → skipped
# ---------------------------------------------------------------------------


def test_load_dir_skips_schema_json(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    _register('eng', 'act')
    _write_yaml(tmp_path, 'pipe.yaml', _MINIMAL_YAML)
    # Place a schema.json — it must not be globbed (*.yaml won't match it)
    (tmp_path / 'schema.json').write_text('{}', encoding='utf-8')

    result = loader.load_dir(tmp_path)
    assert list(result.keys()) == ['my_pipe']


# ---------------------------------------------------------------------------
# Test 6: structural violation (version: 0) → PipelineSchemaError
# ---------------------------------------------------------------------------


def test_schema_error_version_zero(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    bad = """\
pipeline:
  name: my_pipe
  version: 0
  schema_version: 1
  steps:
    - name: step_a
      engine: eng
      action: act
"""
    path = _write_yaml(tmp_path, 'bad.yaml', bad)
    with pytest.raises(PipelineSchemaError) as exc_info:
        loader.load_file(path)
    assert 'bad.yaml' in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 7: unknown action → PipelineActionRefError
# ---------------------------------------------------------------------------


def test_unknown_action(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    # No action registered at all
    path = _write_yaml(tmp_path, 'pipe.yaml', _MINIMAL_YAML)
    with pytest.raises(PipelineActionRefError) as exc_info:
        loader.load_file(path)
    assert 'pipe.yaml' in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 8: forward requires → PipelineRequiresOrderError
# ---------------------------------------------------------------------------


def test_forward_requires(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    _register('eng', 'act')
    yaml_content = """\
pipeline:
  name: my_pipe
  version: 1
  schema_version: 1
  steps:
    - name: step_a
      engine: eng
      action: act
      requires:
        - step_b
    - name: step_b
      engine: eng
      action: act
"""
    path = _write_yaml(tmp_path, 'pipe.yaml', yaml_content)
    with pytest.raises(PipelineRequiresOrderError) as exc_info:
        loader.load_file(path)
    assert 'pipe.yaml' in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 9: unknown requires (refers to non-existent step) → PipelineRequiresOrderError
# ---------------------------------------------------------------------------


def test_unknown_requires(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    _register('eng', 'act')
    yaml_content = """\
pipeline:
  name: my_pipe
  version: 1
  schema_version: 1
  steps:
    - name: step_a
      engine: eng
      action: act
      requires:
        - nonexistent
"""
    path = _write_yaml(tmp_path, 'pipe.yaml', yaml_content)
    with pytest.raises(PipelineRequiresOrderError) as exc_info:
        loader.load_file(path)
    assert 'pipe.yaml' in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 10: ${steps.X.result.Y} where X not in transitive requires → PipelineTemplatingError
# ---------------------------------------------------------------------------


def test_template_step_not_in_requires(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    _register('eng', 'act')
    yaml_content = """\
pipeline:
  name: my_pipe
  version: 1
  schema_version: 1
  steps:
    - name: step_a
      engine: eng
      action: act
    - name: step_b
      engine: eng
      action: act
      args:
        value: "${steps.step_a.result.output}"
"""
    # step_b uses step_a's result but does NOT list step_a in requires
    path = _write_yaml(tmp_path, 'pipe.yaml', yaml_content)
    with pytest.raises(PipelineTemplatingError) as exc_info:
        loader.load_file(path)
    assert 'pipe.yaml' in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 11: ${args.X} where X not in pipeline.args.properties → PipelineTemplatingError
# ---------------------------------------------------------------------------


def test_template_undeclared_arg(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    _register('eng', 'act')
    yaml_content = """\
pipeline:
  name: my_pipe
  version: 1
  schema_version: 1
  args:
    type: object
    properties:
      tenant_id:
        type: string
  steps:
    - name: step_a
      engine: eng
      action: act
      args:
        value: "${args.nonexistent}"
"""
    path = _write_yaml(tmp_path, 'pipe.yaml', yaml_content)
    with pytest.raises(PipelineTemplatingError) as exc_info:
        loader.load_file(path)
    assert 'pipe.yaml' in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 12: two schedule triggers → PipelineTriggerError
# ---------------------------------------------------------------------------


def test_two_schedule_triggers(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    _register('eng', 'act')
    yaml_content = """\
pipeline:
  name: my_pipe
  version: 1
  schema_version: 1
  triggers:
    - type: schedule
      cron: "0 * * * *"
    - type: schedule
      every: 30m
  steps:
    - name: step_a
      engine: eng
      action: act
"""
    path = _write_yaml(tmp_path, 'pipe.yaml', yaml_content)
    with pytest.raises(PipelineTriggerError) as exc_info:
        loader.load_file(path)
    assert 'pipe.yaml' in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 13: schedule trigger args not subset of pipeline args → PipelineTriggerError
# ---------------------------------------------------------------------------


def test_schedule_trigger_args_not_subset(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    _register('eng', 'act')
    yaml_content = """\
pipeline:
  name: my_pipe
  version: 1
  schema_version: 1
  args:
    type: object
    properties:
      tenant_id:
        type: string
  triggers:
    - type: schedule
      cron: "0 * * * *"
      args:
        unknown_key: value
  steps:
    - name: step_a
      engine: eng
      action: act
"""
    path = _write_yaml(tmp_path, 'pipe.yaml', yaml_content)
    with pytest.raises(PipelineTriggerError) as exc_info:
        loader.load_file(path)
    assert 'pipe.yaml' in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 14: invalid YAML syntax → PipelineLoadError with path in message
# ---------------------------------------------------------------------------


def test_invalid_yaml_syntax(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    path = tmp_path / 'broken.yaml'
    path.write_bytes(b'pipeline:\n  name: [\x00bad')
    with pytest.raises(PipelineLoadError) as exc_info:
        loader.load_file(path)
    assert 'broken.yaml' in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 15: YAML root is a list, not mapping → PipelineLoadError
# ---------------------------------------------------------------------------


def test_yaml_root_is_list(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    path = _write_yaml(tmp_path, 'list.yaml', '- a\n- b\n')
    with pytest.raises(PipelineLoadError) as exc_info:
        loader.load_file(path)
    assert 'list.yaml' in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 16: duplicate pipeline.name across files → PipelineLoadError
#          + idempotency: two load_dir calls on clean dir return equal dicts
# ---------------------------------------------------------------------------


def test_duplicate_pipeline_name_across_files(tmp_path: Path, loader: PipelineDefinitionLoader) -> None:
    _register('eng', 'act')

    # Both files have the same pipeline name
    _write_yaml(tmp_path, 'alpha.yaml', _MINIMAL_YAML)
    _write_yaml(tmp_path, 'beta.yaml', _MINIMAL_YAML)  # same name: my_pipe

    with pytest.raises(PipelineLoadError) as exc_info:
        loader.load_dir(tmp_path)

    err_msg = str(exc_info.value)
    assert 'my_pipe' in err_msg
    # At least one of the filenames must be mentioned
    assert 'alpha.yaml' in err_msg or 'beta.yaml' in err_msg

    # Idempotency: clean dir with unique names produces equal results on two calls
    clean = tmp_path / 'clean'
    clean.mkdir()
    _write_yaml(clean, 'alpha.yaml', _MINIMAL_YAML)
    beta_yaml = _MINIMAL_YAML.replace('name: my_pipe', 'name: beta_pipe')
    _write_yaml(clean, 'beta.yaml', beta_yaml)

    result_a = loader.load_dir(clean)
    result_b = loader.load_dir(clean)
    assert set(result_a.keys()) == set(result_b.keys())
    for name in result_a:
        assert result_a[name].content_hash == result_b[name].content_hash
