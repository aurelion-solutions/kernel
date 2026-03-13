# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from enum import StrEnum


class Action(StrEnum):
    """Closed vocabulary of normalized actions for access facts."""

    read = 'read'
    write = 'write'
    execute = 'execute'
    approve = 'approve'
    administer = 'administer'
