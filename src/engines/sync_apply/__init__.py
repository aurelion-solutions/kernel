# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""sync_apply capability slice — public re-exports."""

from src.engines.sync_apply.exceptions import (
    SyncApplyAlreadyExecutedError,
    SyncApplyDeltaItemNotApplicableError,
    SyncApplyInvalidModeError,
    SyncApplyRunNotFoundError,
)
from src.engines.sync_apply.lake_writer import (
    LakeWriterError,
    PreflightRecoveryResult,
    RunWriteResult,
    preflight_recover_already_written,
    write_run_batch,
)
from src.engines.sync_apply.models import (
    SyncApplyResult,
    SyncApplyResultStatus,
    SyncApplyRun,
    SyncApplyRunMode,
    SyncApplyRunStatus,
)
from src.engines.sync_apply.schemas import (
    SyncApplyApplyRequest,
    SyncApplyApplyResponse,
)
from src.engines.sync_apply.service import SyncApplyService

# Side-effect import: registers sync_apply actions in ACTION_REGISTRY at import time.
from src.engines.sync_apply import actions as _actions  # noqa: F401, E402

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
]
