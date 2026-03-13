# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from dataclasses import dataclass
from typing import TypeVar

T = TypeVar('T')


@dataclass(frozen=True)
class ServiceResult[T]:
    ok: bool
    value: T | None = None
    error: str | None = None

    @staticmethod
    def success(value: T) -> 'ServiceResult[T]':
        return ServiceResult[T](ok=True, value=value)

    @staticmethod
    def fail(error: str) -> 'ServiceResult[T]':
        return ServiceResult[T](ok=False, error=error)
