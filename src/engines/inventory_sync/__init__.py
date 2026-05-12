# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""sync_apply capability slice — public re-exports."""

from src.engines.inventory_sync.exceptions import (
    SyncApplyAlreadyExecutedError,
    SyncApplyDeltaItemNotApplicableError,
    SyncApplyInvalidModeError,
    SyncApplyRunNotFoundError,
)
from src.engines.inventory_sync.lake_writer import (
    LakeWriterError,
    PreflightRecoveryResult,
    RunWriteResult,
    preflight_recover_already_written,
    write_run_batch,
)
from src.engines.inventory_sync.models import (
    SyncApplyResult,
    SyncApplyResultStatus,
    SyncApplyRun,
    SyncApplyRunMode,
    SyncApplyRunStatus,
)
from src.engines.inventory_sync.schemas import (
    FactDescriptor,
    SingleFactSyncOp,
    SyncApplyApplyRequest,
    SyncApplyApplyResponse,
)
from src.engines.inventory_sync.service import SyncApplyService

# Side-effect import: registers inventory_sync actions in ACTION_REGISTRY at import time.
from src.engines.inventory_sync import actions as _actions  # noqa: F401, E402

__all__ = [
    'SyncApplyService',
    'SyncApplyApplyRequest',
    'SyncApplyApplyResponse',
    'SyncApplyRunNotFoundError',
    'SyncApplyAlreadyExecutedError',
    'SyncApplyDeltaItemNotApplicableError',
    'SyncApplyInvalidModeError',
    'SyncApplyRun',
    'SyncApplyResult',
    'SyncApplyRunStatus',
    'SyncApplyRunMode',
    'SyncApplyResultStatus',
    'LakeWriterError',
    'RunWriteResult',
    'PreflightRecoveryResult',
    'write_run_batch',
    'preflight_recover_already_written',
    'FactDescriptor',
    'SingleFactSyncOp',
]
