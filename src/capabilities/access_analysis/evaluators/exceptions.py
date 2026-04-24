# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""What-if evaluator exceptions.

All five concrete errors subclass WhatIfValidationError (a ValueError) so that
translate_service_errors({...}) can map each type individually to HTTP 422 without
importing FastAPI types into the service layer.
"""

from __future__ import annotations


class WhatIfValidationError(ValueError):
    """Base class for all what-if validation errors."""


class WhatIfCapabilityNotFoundError(WhatIfValidationError):
    """Raised when a capability_override references a non-existent capability_id."""

    def __init__(self, capability_id: int) -> None:
        self.capability_id = capability_id
        super().__init__(f'Capability {capability_id} not found')


class WhatIfScopeKeyNotFoundError(WhatIfValidationError):
    """Raised when a capability_override references a non-existent scope_key_id."""

    def __init__(self, scope_key_id: int) -> None:
        self.scope_key_id = scope_key_id
        super().__init__(f'ScopeKey {scope_key_id} not found')


class WhatIfApplicationNotFoundError(WhatIfValidationError):
    """Raised when a capability_override references a non-existent application_id."""

    def __init__(self, application_id: object) -> None:
        self.application_id = application_id
        super().__init__(f'Application {application_id} not found')


class WhatIfScopeValueMismatchError(WhatIfValidationError):
    """Raised when scope_value presence does not match the GLOBAL/non-GLOBAL nature of scope_key_id.

    - scope_value is None but scope_key_id is NOT the GLOBAL key → must supply a value
    - scope_value is not None but scope_key_id IS the GLOBAL key → must omit value
    """

    def __init__(self, scope_key_id: int, scope_value: str | None) -> None:
        self.scope_key_id = scope_key_id
        self.scope_value = scope_value
        super().__init__(
            f'scope_value mismatch for scope_key_id={scope_key_id}: '
            f"scope_value={scope_value!r} is inconsistent with the key's global flag"
        )


class WhatIfScopeValueInvalidError(WhatIfValidationError):
    """Raised when scope_value is not normalized (whitespace, uppercase, or > 255 chars)."""

    def __init__(self, scope_value: str) -> None:
        self.scope_value = scope_value
        super().__init__(
            f'scope_value {scope_value!r} is not normalized '
            '(must be lowercase, stripped of whitespace, and at most 255 chars)'
        )
