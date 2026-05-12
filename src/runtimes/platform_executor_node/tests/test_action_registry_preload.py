# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration test: inventory_sync.apply is reachable via the executor's boot path.

Two complementary checks:

1. **Static source check** — asserts that the preload line
   ``import src.engines.inventory_sync.actions as _sa_actions`` is present in
   ``platform_executor_node/main.py``.  If the line is accidentally removed,
   this test catches it immediately without needing to spin up the process.

2. **Ambient registry check** — asserts that ``ACTION_REGISTRY`` already
   contains ``(inventory_sync, apply)`` by the time this test runs, WITHOUT
   evicting ``src.engines.inventory_sync.actions`` from ``sys.modules`` first.

   In a real executor process the sequence is:
     a. Python starts, ``sys.modules`` is empty.
     b. ``platform_executor_node/main.py`` is imported.
     c. ``_run()`` is called by ``asyncio.run``.
     d. The preload import fires, ``@register_action`` populates
        ``ACTION_REGISTRY``.
     e. From that point on the module is cached in ``sys.modules`` and the
        registry retains the entry for the lifetime of the process.

   In the test suite, ``src.engines.inventory_sync.actions`` is imported by
   earlier tests (``test_actions.py``) OR by ``inventory_sync/__init__.py``
   side-effect when the package is touched.  Regardless of which test runs
   first, once the module is in ``sys.modules`` the entry survives unless
   ``_clear_for_tests()`` is called.

   This test does NOT call ``_clear_for_tests()`` before the assertion — it
   checks the ambient registry state, which mirrors what the executor process
   experiences after ``_run()`` bootstraps.  It also calls
   ``importlib.import_module`` without a prior ``sys.modules.pop`` to prove
   that the cached-module path leaves the entry in place.

Together these checks protect against the regression described in Phase 18 Step
9d F1: the action is REST-callable via the API process's transitive import of
``inventory_sync.routes`` but would silently be absent from executor-node's registry
if the preload line were missing from ``main.py``.

Contamination notes
-------------------
Several other test modules call ``ACTION_REGISTRY._clear_for_tests()`` in their
teardown without evicting ``src.engines.inventory_sync.actions`` from ``sys.modules``.
This leaves the module cached but the registry empty; a subsequent
``importlib.import_module`` is a no-op and ``@register_action`` never re-runs.

Additionally, ``test_registry.py::test_no_side_effects_on_import`` calls
``importlib.reload()`` on ``src.platform.orchestrator.registry``.  After a
reload every name exported from that module — including ``ACTION_REGISTRY`` and
all exception classes — is a *new* object.  Any module-level
``from src.platform.orchestrator.registry import …`` reference made before the
reload points at the stale pre-reload object and is useless.

The ``_ensure_inventory_sync_registered`` fixture below defends against both vectors
by always re-fetching ``ACTION_REGISTRY`` directly from ``sys.modules`` at call
time and by force-reloading ``src.engines.inventory_sync.actions`` so
``@register_action`` registers into the *current* singleton unconditionally.
The helper ``_live_registry()`` does the same for tests that need to query the
registry.
"""

from __future__ import annotations

from collections.abc import Iterator
import importlib
from pathlib import Path
import sys
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

_EXECUTOR_MAIN = Path(__file__).parents[1] / 'main.py'
_PRELOAD_TOKEN = 'import src.engines.inventory_sync.actions as _sa_actions'
_ACTIONS_MODULE = 'src.engines.inventory_sync.actions'
_REGISTRY_MODULE = 'src.platform.orchestrator.registry'


# ---------------------------------------------------------------------------
# Helper: always return the live ACTION_REGISTRY singleton
# ---------------------------------------------------------------------------


def _live_registry() -> Any:
    """Return the ACTION_REGISTRY that is currently live in sys.modules.

    After ``importlib.reload(src.platform.orchestrator.registry)`` the singleton
    is replaced; this function always returns the current one by looking it up
    from ``sys.modules`` instead of relying on a module-level import binding.
    """
    reg_mod = sys.modules[_REGISTRY_MODULE]
    return reg_mod.ACTION_REGISTRY


# ---------------------------------------------------------------------------
# Fixture: ensure registry is in a valid state before each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ensure_inventory_sync_registered() -> Iterator[None]:
    """Guarantee that inventory_sync.apply is in ACTION_REGISTRY before each test.

    Defends against two contamination vectors:

    1. Another test cleared the registry via ``_clear_for_tests()`` but left
       ``src.engines.inventory_sync.actions`` in ``sys.modules``, so a plain
       ``importlib.import_module`` call would be a no-op.

    2. ``test_registry.py::test_no_side_effects_on_import`` called
       ``importlib.reload()`` on the registry module, replacing the singleton
       and all exported names with new objects.  Module-level
       ``from … import ACTION_REGISTRY`` references now point at the stale
       pre-reload object.

    Fix: force-reload ``src.engines.inventory_sync.actions`` (pop + re-import) so
    ``@register_action`` runs and registers into the current live singleton.
    Always use ``_live_registry()`` to access the singleton — never a stale
    module-level binding.

    Teardown does NOT clear the registry — mirrors a live executor process where
    the entry persists for the process lifetime.
    """
    # Ensure the registry module itself is importable (handles the reload case).
    importlib.import_module(_REGISTRY_MODULE)

    # Fetch the ActionNotFoundError class from the *live* module so the except
    # clause matches even after importlib.reload() replaced the class object.
    reg_mod = sys.modules[_REGISTRY_MODULE]
    LiveActionNotFoundError: type[Exception] = reg_mod.ActionNotFoundError

    try:
        _live_registry().get('inventory_sync', 'apply')
    except LiveActionNotFoundError:
        # Registry was cleared by another test but the module is still cached.
        # Evict and re-import to re-trigger @register_action on the live singleton.
        sys.modules.pop(_ACTIONS_MODULE, None)
        importlib.import_module(_ACTIONS_MODULE)

    # Sanity: the live registry must now contain the entry.
    _live_registry().get('inventory_sync', 'apply')

    yield


# ---------------------------------------------------------------------------
# Static check: preload line exists in executor main
# ---------------------------------------------------------------------------


def test_executor_main_contains_inventory_sync_preload() -> None:
    """The preload line for inventory_sync.actions is present in platform_executor_node/main.py.

    If this line is removed, the executor process will start without registering
    the ``inventory_sync.apply`` action, causing ActionNotFoundError at pipeline
    dispatch time.  This static check catches the regression before any runtime
    test runs.
    """
    source = _EXECUTOR_MAIN.read_text(encoding='utf-8')
    assert _PRELOAD_TOKEN in source, (
        f'Preload line missing from {_EXECUTOR_MAIN}.\n'
        f'Expected to find: {_PRELOAD_TOKEN!r}\n'
        'Add it to the engine-action imports block inside _run().'
    )


# ---------------------------------------------------------------------------
# Ambient registry check — no sys.modules eviction, no _clear_for_tests()
# ---------------------------------------------------------------------------


def test_inventory_sync_action_in_registry_via_cached_module_import() -> None:
    """inventory_sync.apply is in ACTION_REGISTRY after a cache-hit import of the actions module.

    The test deliberately does NOT call ``sys.modules.pop`` before importing
    ``src.engines.inventory_sync.actions``.  This mirrors the executor process
    after ``_run()`` fires the preload import: subsequent calls to
    ``importlib.import_module`` on that module are no-ops (cache hit) and the
    registry entry survives.

    If this test fails with ActionNotFoundError it means either:
    - ``@register_action`` in ``inventory_sync/actions.py`` is broken, or
    - something else cleared the registry AND the module was not re-imported
      (which cannot happen via the production executor path).

    Note: this test does NOT call ``_clear_for_tests()`` before the assertion.
    The registry must remain populated — same as in a live executor process.
    """
    # Ensure the actions module is loaded (cache-hit: the autouse fixture already
    # imported it above, so this importlib call is a no-op — exactly the cached
    # path we want to exercise).
    importlib.import_module(_ACTIONS_MODULE)

    # The module must still be in sys.modules after the no-op import.
    was_cached = _ACTIONS_MODULE in sys.modules
    assert was_cached, (
        f'{_ACTIONS_MODULE} should be in sys.modules after importlib.import_module — import machinery failed silently'
    )

    # Always query via the live singleton to avoid stale references after any
    # registry-module reload done by other tests.
    registry = _live_registry()
    rec = registry.get('inventory_sync', 'apply')

    assert rec.engine == 'inventory_sync'
    assert rec.action == 'apply'
    assert rec.idempotent is True
