# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Single source of truth for pipeline cartridge directories.

Both runtimes — the FastAPI process (``platform_api``) and the standalone
executor (``platform_executor_node``) — need to load the same set of
pipeline cartridges. Keeping the path list here prevents drift between the
two and keeps the cross-layer reference (platform → product cartridges)
isolated to a single location that can be replaced with discovery-based
resolution without touching every runtime.

When a third product ships (Lens / Pulse / …) and starts owning cartridges,
this module is the only file that changes — preferably by moving the
hard-coded paths behind an env variable like ``AURELION_CARTRIDGE_DIRS``.
"""

from __future__ import annotations

from pathlib import Path

# ``aurelion-kernel/src/platform/orchestrator/`` → ``aurelion-kernel/``.
_KERNEL_ROOT = Path(__file__).parent.parent.parent.parent

# Kernel-shipped infrastructure pipelines (access_apply, reconciliation, …).
_KERNEL_PIPELINES_DIR = _KERNEL_ROOT / 'pipelines'

# Product cartridges live at the monorepo root, sibling to the kernel package,
# so multiple products can ship cartridges in one common tree. A missing
# directory is a no-op — the loader returns ``{}`` for non-existent paths.
_PRODUCT_CARTRIDGES_ROOT = _KERNEL_ROOT.parent / 'cartridges'

# Concrete product cartridge directories. Adding a new product is one line.
_JOURNEY_CARTRIDGES_DIR = _PRODUCT_CARTRIDGES_ROOT / 'journey'

# Canonical ordered tuple consumed by both runtimes. Duplicate ``pipeline.name``
# across directories is rejected by ``PipelineDefinitionLoader.load_many``.
PIPELINE_SOURCE_DIRS: tuple[Path, ...] = (
    _KERNEL_PIPELINES_DIR,
    _JOURNEY_CARTRIDGES_DIR,
)
