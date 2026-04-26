# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Business logic for LLMModel and LLMExecutionProfile CRUD.

Service discipline
------------------
* Services flush; routes commit (ARCH_CONTEXT.md).
* ``IntegrityError`` translation lives in ``_translate_integrity_error`` /
  ``_translate_profile_integrity_error``.
* Inline validation lives in ``_validate_*`` / ``_reject_*`` module-level helpers.
* ``factory.invalidate(model_id)`` is called post-flush, pre-commit.

Cache invalidation trade-off
----------------------------
Invalidation happens after ``session.flush()`` but before the caller's
``session.commit()``.  If the commit later fails the cache has already been
wiped while the DB row reverts to its pre-update state.  This is acceptable:
on the next inference request the factory rehydrates straight from the DB
(the source of truth).  The reverse ordering (commit then invalidate) is
strictly worse — a crash between commit and invalidate pins a stale provider
in memory with no eviction signal.

PATCH semantics
---------------
``update_llm_model`` / ``update_llm_profile`` iterate over
``request.model_fields_set`` (not ``model_dump(exclude_none=True)``) so that
explicitly setting a nullable field to ``null`` clears it on the row.
``_reject_*_null_on_not_null_fields`` guards the NOT NULL columns before flush.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, NoReturn
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.llm.exceptions import (
    LLMModelInvalidConfigError,
    LLMModelLocalPathUnreadableError,
    LLMModelNameAlreadyExistsError,
    LLMModelNotFoundError,
    LLMProfileInvalidConfigError,
    LLMProfileNameAlreadyExistsError,
    LLMProfileNotFoundError,
)
from src.platform.llm.models import LLMExecutionProfile, LLMModel, LLMProvider
from src.platform.llm.repository import get_by_id, get_profile_by_id, list_all, list_profiles
from src.platform.llm.schemas import (
    LLMExecutionProfileCreate,
    LLMExecutionProfileUpdate,
    LLMModelCreate,
    LLMModelUpdate,
)
from src.platform.logs.schemas import LogLevel, LogParticipantKind
from src.platform.logs.service import LogService, NoOpLogService

if TYPE_CHECKING:
    from src.platform.llm.factory import LLMFactory

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_SYSTEM = LogParticipantKind.SYSTEM.value


def _validate_provider_wiring(
    provider: LLMProvider,
    local_path: str | None,
    endpoint_url: str | None,
    model_ref: str | None,
    secret_id: uuid.UUID | None,
) -> None:
    """Raise ``LLMModelInvalidConfigError`` on provider/field mismatches."""
    if provider == LLMProvider.llama_cpp:
        if not local_path:
            raise LLMModelInvalidConfigError('llama_cpp provider requires local_path')
        if endpoint_url is not None:
            raise LLMModelInvalidConfigError('llama_cpp provider must not set endpoint_url')
        if model_ref is not None:
            raise LLMModelInvalidConfigError('llama_cpp provider must not set model_ref')
        if secret_id is not None:
            raise LLMModelInvalidConfigError('llama_cpp provider must not set secret_id')

    elif provider == LLMProvider.openai:
        if not endpoint_url:
            raise LLMModelInvalidConfigError('openai provider requires endpoint_url')
        if not model_ref:
            raise LLMModelInvalidConfigError('openai provider requires model_ref')
        if secret_id is None:
            raise LLMModelInvalidConfigError('openai provider requires secret_id')
        if local_path is not None:
            raise LLMModelInvalidConfigError('openai provider must not set local_path')

    elif provider == LLMProvider.ollama:
        if not endpoint_url:
            raise LLMModelInvalidConfigError('ollama provider requires endpoint_url')
        if not model_ref:
            raise LLMModelInvalidConfigError('ollama provider requires model_ref')
        if local_path is not None:
            raise LLMModelInvalidConfigError('ollama provider must not set local_path')


def _validate_token_limits(context_window: int | None, max_total_tokens: int | None) -> None:
    """Raise ``LLMModelInvalidConfigError`` if token limits are incoherent."""
    if context_window is not None and max_total_tokens is not None:
        if max_total_tokens > context_window:
            raise LLMModelInvalidConfigError(
                f'max_total_tokens ({max_total_tokens}) must be <= context_window ({context_window})'
            )


async def _validate_local_path_readable(local_path: str | None) -> None:
    """Raise ``LLMModelLocalPathUnreadableError`` if path missing or not readable.

    Uses ``asyncio.to_thread`` to avoid blocking the event loop on filesystem calls.
    Only invoked for ``llama_cpp`` provider where ``local_path`` is set.
    """
    if local_path is None:
        return

    def _check() -> bool:
        return os.path.exists(local_path) and os.access(local_path, os.R_OK)

    readable = await asyncio.to_thread(_check)
    if not readable:
        raise LLMModelLocalPathUnreadableError(f'local_path is not readable: {local_path}')


# ---------------------------------------------------------------------------
# NOT NULL guard helpers (service-layer validation before flush)
# ---------------------------------------------------------------------------

_LLM_MODEL_NOT_NULL_FIELDS = ('name', 'default_params', 'is_active')
_LLM_PROFILE_NOT_NULL_FIELDS = ('name', 'param_overrides')


def _reject_model_null_on_not_null_fields(request: LLMModelUpdate) -> None:
    """Raise ``LLMModelInvalidConfigError`` when a NOT NULL column is set to None."""
    for field in _LLM_MODEL_NOT_NULL_FIELDS:
        if field in request.model_fields_set and getattr(request, field) is None:
            raise LLMModelInvalidConfigError(f"'{field}' cannot be null")


def _reject_profile_null_on_not_null_fields(request: LLMExecutionProfileUpdate) -> None:
    """Raise ``LLMProfileInvalidConfigError`` when a NOT NULL column is set to None."""
    for field in _LLM_PROFILE_NOT_NULL_FIELDS:
        if field in request.model_fields_set and getattr(request, field) is None:
            raise LLMProfileInvalidConfigError(f"'{field}' cannot be null")


# ---------------------------------------------------------------------------
# Integrity error translation
# ---------------------------------------------------------------------------


def _translate_integrity_error(exc: IntegrityError, name: str) -> NoReturn:
    """Translate DB constraint violations to domain errors; re-raise unknowns."""
    orig = exc.orig
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    asyncpg_exc = getattr(orig, '__cause__', None)
    constraint: str | None = getattr(asyncpg_exc, 'constraint_name', None)

    if pgcode == '23505' and constraint == 'uq_llm_models_name':
        raise LLMModelNameAlreadyExistsError(f"LLMModel with name '{name}' already exists") from None

    if pgcode in ('23503', '23000'):
        if constraint == 'llm_execution_profiles_model_id_fkey':
            # ON DELETE RESTRICT — model has dependent profiles blocking delete
            raise LLMModelInvalidConfigError('model has dependent execution profiles') from None
        if constraint == 'llm_models_secret_id_fkey':
            # CREATE/UPDATE with a secret_id that does not exist
            raise LLMModelInvalidConfigError('secret_id references a non-existent secret') from None
        # Unknown FK — surface to the caller without masking
        raise exc

    raise exc


def _translate_profile_integrity_error(exc: IntegrityError, name: str) -> NoReturn:
    """Translate DB constraint violations for profile writes to domain errors."""
    orig = exc.orig
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    asyncpg_exc = getattr(orig, '__cause__', None)
    constraint: str | None = getattr(asyncpg_exc, 'constraint_name', None)

    if pgcode == '23505' and constraint == 'uq_llm_execution_profiles_name':
        raise LLMProfileNameAlreadyExistsError(f"LLMExecutionProfile with name '{name}' already exists") from None

    if pgcode in ('23503', '23000') and constraint in (
        'llm_execution_profiles_model_id_fkey',
        'fk_llm_execution_profiles_model_id_llm_models',
    ):
        raise LLMProfileInvalidConfigError('model_id references a non-existent LLMModel') from None

    raise exc


# ---------------------------------------------------------------------------
# Service callables
# ---------------------------------------------------------------------------


async def create_llm_model(
    session: AsyncSession,
    request: LLMModelCreate,
    *,
    log_service: LogService | None = None,
) -> LLMModel:
    """Create and persist an LLMModel row.

    Validates provider wiring, token limits, and (for llama_cpp) path
    readability before inserting.  Flushes but does NOT commit.
    """
    log = log_service if log_service is not None else NoOpLogService()

    _validate_provider_wiring(
        request.provider,
        request.local_path,
        request.endpoint_url,
        request.model_ref,
        request.secret_id,
    )
    _validate_token_limits(request.context_window, request.max_total_tokens)
    if request.provider == LLMProvider.llama_cpp:
        await _validate_local_path_readable(request.local_path)

    model = LLMModel(
        name=request.name,
        description=request.description,
        provider=request.provider,
        local_path=request.local_path,
        endpoint_url=request.endpoint_url,
        model_ref=request.model_ref,
        context_window=request.context_window,
        max_total_tokens=request.max_total_tokens,
        default_params=request.default_params,
        secret_id=request.secret_id,
        is_active=request.is_active,
    )
    session.add(model)
    try:
        await session.flush()
    except IntegrityError as exc:
        _translate_integrity_error(exc, request.name)
    await session.refresh(model)

    log.emit_safe(
        level=LogLevel.INFO,
        message='LLMModel created',
        component='llm_models',
        payload={
            'model_id': str(model.id),
            'name': model.name,
            'provider': model.provider.value,
            'initiator_type': _SYSTEM,
            'initiator_id': 'llm_models',
            'actor_type': _SYSTEM,
            'actor_id': 'llm_models',
            'target_type': _SYSTEM,
            'target_id': str(model.id),
        },
    )
    return model


async def get_llm_model(
    session: AsyncSession,
    model_id: uuid.UUID,
) -> LLMModel | None:
    """Return an LLMModel by id, or None."""
    return await get_by_id(session, model_id)


async def list_llm_models(session: AsyncSession) -> list[LLMModel]:
    """Return all LLMModel rows sorted by name ascending."""
    return await list_all(session)


async def update_llm_model(
    session: AsyncSession,
    model_id: uuid.UUID,
    request: LLMModelUpdate,
    *,
    factory: LLMFactory,
    log_service: LogService | None = None,
) -> LLMModel:
    """Apply a partial update to an LLMModel row.

    PATCH semantics: iterates ``request.model_fields_set`` so that
    setting a field to ``null`` explicitly clears the column.  Fields
    absent from the request are left untouched.

    Cache invalidation fires post-flush, pre-return when:
    - ``is_active`` changes ``True → False``;
    - ``local_path``, ``endpoint_url``, or ``model_ref`` changes.
    """
    log = log_service if log_service is not None else NoOpLogService()

    model = await get_by_id(session, model_id)
    if model is None:
        raise LLMModelNotFoundError(f'LLMModel {model_id} not found')

    _reject_model_null_on_not_null_fields(request)

    # Snapshot pre-update values for invalidation predicate
    prev_is_active = model.is_active
    prev_local_path = model.local_path
    prev_endpoint_url = model.endpoint_url
    prev_model_ref = model.model_ref

    # Apply only fields present in the request
    changed_fields: list[str] = []
    for field in request.model_fields_set:
        value = getattr(request, field)
        setattr(model, field, value)
        changed_fields.append(field)

    # Re-validate after merge
    _validate_provider_wiring(
        model.provider,
        model.local_path,
        model.endpoint_url,
        model.model_ref,
        model.secret_id,
    )
    _validate_token_limits(model.context_window, model.max_total_tokens)
    if model.provider == LLMProvider.llama_cpp:
        await _validate_local_path_readable(model.local_path)

    try:
        await session.flush()
    except IntegrityError as exc:
        _translate_integrity_error(exc, model.name)
    await session.refresh(model)

    # Cache invalidation (post-flush, pre-return)
    should_invalidate = (
        (prev_is_active and not model.is_active)
        or model.local_path != prev_local_path
        or model.endpoint_url != prev_endpoint_url
        or model.model_ref != prev_model_ref
    )
    if should_invalidate:
        await factory.invalidate(model_id)

    log.emit_safe(
        level=LogLevel.INFO,
        message='LLMModel updated',
        component='llm_models',
        payload={
            'model_id': str(model.id),
            'name': model.name,
            'provider': model.provider.value,
            'changed_fields': changed_fields,
            'initiator_type': _SYSTEM,
            'initiator_id': 'llm_models',
            'actor_type': _SYSTEM,
            'actor_id': 'llm_models',
            'target_type': _SYSTEM,
            'target_id': str(model.id),
        },
    )
    return model


async def delete_llm_model(
    session: AsyncSession,
    model_id: uuid.UUID,
    *,
    factory: LLMFactory,
    log_service: LogService | None = None,
) -> None:
    """Hard-delete an LLMModel row.

    Raises ``LLMModelNotFoundError`` if not found.
    Raises ``LLMModelInvalidConfigError`` if dependent profiles exist (FK RESTRICT).
    Invalidates the factory cache after successful flush.
    """
    log = log_service if log_service is not None else NoOpLogService()

    model = await get_by_id(session, model_id)
    if model is None:
        raise LLMModelNotFoundError(f'LLMModel {model_id} not found')

    model_name = model.name
    model_provider = model.provider.value

    await session.delete(model)
    try:
        await session.flush()
    except IntegrityError as exc:
        _translate_integrity_error(exc, model_name)

    # Invalidate even if row is gone — in-process cache may still hold provider
    await factory.invalidate(model_id)

    log.emit_safe(
        level=LogLevel.INFO,
        message='LLMModel deleted',
        component='llm_models',
        payload={
            'model_id': str(model_id),
            'name': model_name,
            'provider': model_provider,
            'initiator_type': _SYSTEM,
            'initiator_id': 'llm_models',
            'actor_type': _SYSTEM,
            'actor_id': 'llm_models',
            'target_type': _SYSTEM,
            'target_id': str(model_id),
        },
    )


# ---------------------------------------------------------------------------
# LLMExecutionProfile service callables
# ---------------------------------------------------------------------------


async def create_llm_profile(
    session: AsyncSession,
    request: LLMExecutionProfileCreate,
    *,
    log_service: LogService | None = None,
) -> LLMExecutionProfile:
    """Create and persist an LLMExecutionProfile row.

    Raises ``LLMProfileNameAlreadyExistsError`` on duplicate name.
    Raises ``LLMProfileInvalidConfigError`` when ``model_id`` FK is dangling.
    Flushes but does NOT commit.
    """
    log = log_service if log_service is not None else NoOpLogService()

    profile = LLMExecutionProfile(
        name=request.name,
        model_id=request.model_id,
        param_overrides=request.param_overrides,
    )
    session.add(profile)
    try:
        await session.flush()
    except IntegrityError as exc:
        _translate_profile_integrity_error(exc, request.name)
    await session.refresh(profile)

    log.emit_safe(
        level=LogLevel.INFO,
        message='LLMExecutionProfile created',
        component='llm_profiles',
        payload={
            'profile_id': str(profile.id),
            'name': profile.name,
            'model_id': str(profile.model_id),
            'initiator_type': _SYSTEM,
            'initiator_id': 'llm_profiles',
            'actor_type': _SYSTEM,
            'actor_id': 'llm_profiles',
            'target_type': _SYSTEM,
            'target_id': str(profile.id),
        },
    )
    return profile


async def get_llm_profile(
    session: AsyncSession,
    profile_id: uuid.UUID,
) -> LLMExecutionProfile | None:
    """Return an LLMExecutionProfile by id, or None."""
    return await get_profile_by_id(session, profile_id)


async def list_llm_profiles(session: AsyncSession) -> list[LLMExecutionProfile]:
    """Return all LLMExecutionProfile rows sorted by name ascending."""
    return await list_profiles(session)


async def update_llm_profile(
    session: AsyncSession,
    profile_id: uuid.UUID,
    request: LLMExecutionProfileUpdate,
    *,
    log_service: LogService | None = None,
) -> LLMExecutionProfile:
    """Apply a partial update to an LLMExecutionProfile row.

    PATCH semantics: only fields in ``request.model_fields_set`` are applied.
    Raises ``LLMProfileNotFoundError`` if not found.
    Raises ``LLMProfileInvalidConfigError`` when a NOT NULL column is set to None
    or on FK violation.
    Raises ``LLMProfileNameAlreadyExistsError`` on duplicate name.
    """
    log = log_service if log_service is not None else NoOpLogService()

    profile = await get_profile_by_id(session, profile_id)
    if profile is None:
        raise LLMProfileNotFoundError(f'LLMExecutionProfile {profile_id} not found')

    _reject_profile_null_on_not_null_fields(request)

    changed_fields: list[str] = []
    for field in request.model_fields_set:
        setattr(profile, field, getattr(request, field))
        changed_fields.append(field)

    # Snapshot name before flush — ORM object may be in deactive state on IntegrityError
    pending_name: str = profile.name

    try:
        await session.flush()
    except IntegrityError as exc:
        _translate_profile_integrity_error(exc, pending_name)
    await session.refresh(profile)

    log.emit_safe(
        level=LogLevel.INFO,
        message='LLMExecutionProfile updated',
        component='llm_profiles',
        payload={
            'profile_id': str(profile.id),
            'name': profile.name,
            'model_id': str(profile.model_id),
            'changed_fields': changed_fields,
            'initiator_type': _SYSTEM,
            'initiator_id': 'llm_profiles',
            'actor_type': _SYSTEM,
            'actor_id': 'llm_profiles',
            'target_type': _SYSTEM,
            'target_id': str(profile.id),
        },
    )
    return profile


async def delete_llm_profile(
    session: AsyncSession,
    profile_id: uuid.UUID,
    *,
    log_service: LogService | None = None,
) -> None:
    """Hard-delete an LLMExecutionProfile row.

    Raises ``LLMProfileNotFoundError`` if not found.
    Flushes but does NOT commit.
    """
    log = log_service if log_service is not None else NoOpLogService()

    profile = await get_profile_by_id(session, profile_id)
    if profile is None:
        raise LLMProfileNotFoundError(f'LLMExecutionProfile {profile_id} not found')

    profile_name = profile.name
    model_id = profile.model_id

    await session.delete(profile)
    await session.flush()

    log.emit_safe(
        level=LogLevel.INFO,
        message='LLMExecutionProfile deleted',
        component='llm_profiles',
        payload={
            'profile_id': str(profile_id),
            'name': profile_name,
            'model_id': str(model_id),
            'initiator_type': _SYSTEM,
            'initiator_id': 'llm_profiles',
            'actor_type': _SYSTEM,
            'actor_id': 'llm_profiles',
            'target_type': _SYSTEM,
            'target_id': str(profile_id),
        },
    )
