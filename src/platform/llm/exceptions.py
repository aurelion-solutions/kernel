# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Domain exceptions for the LLM model CRUD path.

These exceptions signal CRUD-layer failures, not factory load-time state.
``LLMFactory`` has its own ``LLMModelNotFoundError`` (in ``factory.py``) which
signals runtime provider-cache misses.  The two hierarchies are intentionally
separate; importers disambiguate by module path.
"""


class LLMModelNameAlreadyExistsError(Exception):
    """An LLMModel with the given name already exists."""


class LLMModelNotFoundError(Exception):
    """No LLMModel row with the requested id exists."""


class LLMModelInvalidConfigError(Exception):
    """Provider/path/URL/secret combination is invalid, or a dependent object blocks delete."""


class LLMModelLocalPathUnreadableError(LLMModelInvalidConfigError):
    """The ``local_path`` does not exist or is not readable by the current process."""


# ---------------------------------------------------------------------------
# LLMExecutionProfile CRUD exceptions
# ---------------------------------------------------------------------------


class LLMProfileNameAlreadyExistsError(Exception):
    """An LLMExecutionProfile with the given name already exists."""


class LLMProfileNotFoundError(Exception):
    """No LLMExecutionProfile row with the requested id exists."""


class LLMProfileInvalidConfigError(Exception):
    """Profile config is invalid (null on NOT NULL field, or FK violation)."""


# ---------------------------------------------------------------------------
# Inference exceptions
# ---------------------------------------------------------------------------


class LLMInferenceValidationError(Exception):
    """Inference request violates LLMSettings limits."""
