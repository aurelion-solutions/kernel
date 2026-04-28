# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake slice exceptions."""


class LakeError(Exception):
    """Base exception for all lake slice errors."""


class LakeCatalogError(LakeError):
    """Raised when catalog initialization or namespace operations fail."""


class LakeSessionError(LakeError):
    """Raised when a DuckDB session encounters an error (extension load, bootstrap SQL)."""


class LakeSessionPoolExhaustedError(LakeSessionError):
    """Raised when acquire() cannot get a session before acquire_timeout_seconds elapses."""


class LakeMaintenanceError(LakeError):
    """Raised when compaction, snapshot expiry, or orphan cleanup fails."""
