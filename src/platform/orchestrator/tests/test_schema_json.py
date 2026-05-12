# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure validation tests for aurelion-kernel/pipelines/schema.json.

No database — loads the JSON Schema from disk and runs jsonschema Draft 2020-12
validation against various pipeline documents.

Acceptance gate: test_schema_is_valid_draft_2020_12 must pass before any other
tests in this module are meaningful.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import ValidationError
from jsonschema.validators import Draft202012Validator
import pytest

# ---------------------------------------------------------------------------
# Schema fixture
# ---------------------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).parents[4] / 'pipelines' / 'schema.json'


@pytest.fixture(scope='module')
def schema() -> dict[str, object]:
    """Load the pipeline JSON Schema from disk once per module."""
    result: dict[str, object] = json.load(_SCHEMA_PATH.open())
    return result


@pytest.fixture(scope='module')
def validator(schema: dict[str, object]) -> Draft202012Validator:
    return Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# Acceptance gate
# ---------------------------------------------------------------------------


def test_schema_is_valid_draft_2020_12(schema: dict[str, object]) -> None:
    """The schema itself must be a valid Draft 2020-12 meta-schema document."""
    Draft202012Validator.check_schema(schema)


# ---------------------------------------------------------------------------
# Valid pipeline documents
# ---------------------------------------------------------------------------


def test_minimal_valid_pipeline_passes(validator: Draft202012Validator) -> None:
    """Minimal pipeline with an MQ trigger and one engine-call step must validate."""
    doc = {
        'pipeline': {
            'name': 'foo',
            'version': 1,
            'schema_version': 1,
            'triggers': [{'type': 'mq', 'routing_key': 'x.y'}],
            'steps': [{'name': 'a', 'engine': 'e', 'action': 'act'}],
        }
    }
    validator.validate(doc)  # must not raise


def test_pipeline_no_triggers_valid(validator: Draft202012Validator) -> None:
    """triggers is optional; a pipeline with only steps is valid."""
    doc = {
        'pipeline': {
            'name': 'no_trigger',
            'version': 1,
            'schema_version': 1,
            'steps': [{'name': 'step_one', 'engine': 'myengine', 'action': 'run'}],
        }
    }
    validator.validate(doc)


def test_schedule_trigger_cron_valid(validator: Draft202012Validator) -> None:
    doc = {
        'pipeline': {
            'name': 'sched_cron',
            'version': 1,
            'schema_version': 1,
            'triggers': [{'type': 'schedule', 'cron': '0 * * * *'}],
            'steps': [{'name': 'do_it', 'engine': 'eng', 'action': 'act'}],
        }
    }
    validator.validate(doc)


def test_schedule_trigger_every_valid(validator: Draft202012Validator) -> None:
    doc = {
        'pipeline': {
            'name': 'sched_every',
            'version': 1,
            'schema_version': 1,
            'triggers': [{'type': 'schedule', 'every': '30m'}],
            'steps': [{'name': 'do_it', 'engine': 'eng', 'action': 'act'}],
        }
    }
    validator.validate(doc)


def test_wait_for_event_step_valid(validator: Draft202012Validator) -> None:
    doc = {
        'pipeline': {
            'name': 'wait_pipe',
            'version': 1,
            'schema_version': 1,
            'steps': [
                {'name': 'wait_approval', 'type': 'wait_for_event', 'event': 'approval.granted', 'timeout': '1h'}
            ],
        }
    }
    validator.validate(doc)


# ---------------------------------------------------------------------------
# Invalid pipeline documents
# ---------------------------------------------------------------------------


def test_step_engine_and_wait_for_event_mutually_exclusive_fails(validator: Draft202012Validator) -> None:
    """A step with both engine/action AND type=wait_for_event must fail (oneOf)."""
    doc = {
        'pipeline': {
            'name': 'bad_step',
            'version': 1,
            'schema_version': 1,
            'steps': [
                {
                    'name': 'mixed',
                    'engine': 'someengine',
                    'action': 'do',
                    'type': 'wait_for_event',
                    'event': 'x.y',
                    'timeout': '5m',
                }
            ],
        }
    }
    with pytest.raises(ValidationError):
        validator.validate(doc)


def test_wait_for_event_without_timeout_fails(validator: Draft202012Validator) -> None:
    """A wait_for_event step missing 'timeout' must fail (timeout is REQUIRED)."""
    doc = {
        'pipeline': {
            'name': 'no_timeout',
            'version': 1,
            'schema_version': 1,
            'steps': [{'name': 'wait_it', 'type': 'wait_for_event', 'event': 'x.y'}],
        }
    }
    with pytest.raises(ValidationError):
        validator.validate(doc)


def test_schedule_trigger_requires_cron_xor_every(validator: Draft202012Validator) -> None:
    """Schedule trigger must have exactly one of cron or every."""
    # Neither — fails.
    doc_neither = {
        'pipeline': {
            'name': 'no_sched',
            'version': 1,
            'schema_version': 1,
            'triggers': [{'type': 'schedule'}],
            'steps': [{'name': 's', 'engine': 'e', 'action': 'a'}],
        }
    }
    with pytest.raises(ValidationError):
        validator.validate(doc_neither)

    # Both — fails.
    doc_both = {
        'pipeline': {
            'name': 'both_sched',
            'version': 1,
            'schema_version': 1,
            'triggers': [{'type': 'schedule', 'cron': '0 * * * *', 'every': '1h'}],
            'steps': [{'name': 's', 'engine': 'e', 'action': 'a'}],
        }
    }
    with pytest.raises(ValidationError):
        validator.validate(doc_both)

    # Exactly one (cron) — passes.
    doc_cron = {
        'pipeline': {
            'name': 'ok_cron',
            'version': 1,
            'schema_version': 1,
            'triggers': [{'type': 'schedule', 'cron': '0 * * * *'}],
            'steps': [{'name': 's', 'engine': 'e', 'action': 'a'}],
        }
    }
    validator.validate(doc_cron)


def test_unknown_top_level_property_rejected(validator: Draft202012Validator) -> None:
    """pipeline.foo is rejected by additionalProperties: false."""
    doc = {
        'pipeline': {
            'name': 'extra',
            'version': 1,
            'schema_version': 1,
            'steps': [{'name': 's', 'engine': 'e', 'action': 'a'}],
            'foo': 'bar',
        }
    }
    with pytest.raises(ValidationError):
        validator.validate(doc)


def test_step_name_pattern_enforced(validator: Draft202012Validator) -> None:
    """Step names must match ^[a-z][a-z0-9_]*$."""
    base = {'version': 1, 'schema_version': 1}

    for bad_name in ('Name', '1step', 'with-dash'):
        doc = {
            'pipeline': {
                **base,
                'name': 'pattern_test',
                'steps': [{'name': bad_name, 'engine': 'e', 'action': 'a'}],
            }
        }
        with pytest.raises(ValidationError):
            validator.validate(doc)

    # Valid name must pass.
    doc_ok = {
        'pipeline': {
            **base,
            'name': 'pattern_test',
            'steps': [{'name': 'step_one', 'engine': 'e', 'action': 'a'}],
        }
    }
    validator.validate(doc_ok)


def test_no_http_trigger_type_accepted(validator: Draft202012Validator) -> None:
    """Trigger type 'http' is not valid — manual triggers use POST /pipeline-runs."""
    doc = {
        'pipeline': {
            'name': 'http_trigger',
            'version': 1,
            'schema_version': 1,
            'triggers': [{'type': 'http'}],
            'steps': [{'name': 's', 'engine': 'e', 'action': 'a'}],
        }
    }
    with pytest.raises(ValidationError):
        validator.validate(doc)
