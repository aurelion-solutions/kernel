# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Route-level HTTP error translation utility.

Purpose
-------
Provide a single, explicit, zero-magic mechanism for translating
slice-specific service exceptions into FastAPI ``HTTPException`` responses
inside route handlers.

Mapping shape
-------------
An ``ErrorMap`` is a plain ``dict`` (or any ``Mapping``) keyed by concrete
exception class.  The value is a ``(status_code, detail)`` tuple where
``detail`` is either:

- a **static string**, or
- a **``Callable[[Exception], str]``** — called with the live exception to
  produce the detail string (useful when the detail interpolates exception
  data or closes over request-body variables).

Discriminator: ``isinstance(detail, str)`` — strings are not callable.

Lookup rule
-----------
Iteration follows **insertion order** (Python 3.7+ dict guarantee).
The first key for which ``isinstance(exc, key)`` is ``True`` wins.
Put more-specific (child) classes **before** their parents in the mapping.

``from None`` semantics
-----------------------
On a match the context manager constructs ``HTTPException(status_code,
detail=msg)``, sets ``__cause__ = None`` and ``__suppress_context__ = True``
(exactly what ``raise ... from None`` does at bytecode level), then raises
it.  The chained context (``__context__``) is set by Python's exception
machinery but suppressed for display — behaviour is identical to the
original ``except SomeError: raise HTTPException(...) from None`` blocks.

Non-goals
---------
- Does **not** abstract the service call itself.
- Does **not** handle the ``None → 404`` pattern (``get_x`` handlers keep
  their explicit ``if x is None: raise HTTPException(404, ...)``).
- Does **not** touch route registration or response shaping.
- Does **not** log; FastAPI's access log captures the outcome.
- Does **not** use a global registry, decorator, or middleware.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from fastapi import HTTPException

ErrorDetail = str | Callable[[Exception], str]
ErrorMap = Mapping[type[Exception], tuple[int, ErrorDetail]]


@contextmanager
def translate_service_errors(mapping: ErrorMap) -> Iterator[None]:
    """Translate service exceptions to ``HTTPException`` via an explicit mapping.

    Usage example::

        with translate_service_errors({
            ResourceNotFoundError: (404, 'Resource not found'),
            DuplicateResourceAttributeError: (
                409,
                lambda _exc: f'Attribute key already exists: {body.key}',
            ),
        }):
            result = await service.do_thing(session, ...)
        await session.commit()
        return ResponseSchema.model_validate(result)

    The ``with`` block should wrap only the service call.  ``session.commit()``
    and the return statement live **outside** the block on the happy path.

    Args:
        mapping: ``{ExcClass: (status_code, detail)}`` where ``detail`` is
            either a static string or a callable that receives the exception
            and returns a string.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        for exc_type, (status_code, detail) in mapping.items():
            if isinstance(exc, exc_type):
                msg = detail(exc) if callable(detail) else detail
                raise HTTPException(status_code=status_code, detail=msg) from None
        raise
