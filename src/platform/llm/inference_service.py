# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Inference orchestration for the LLM platform slice.

CRUD lives in service.py; inference orchestration lives here.

Two public coroutine functions:
- ``run_inference`` — accumulates all tokens and returns a single ``InferenceResponse``.
- ``stream_inference`` — yields SSE payload dicts for the SSE route.

Both share the private async generator ``_drive_inference`` which handles:
- profile/model resolution (404 / inactive errors)
- message validation via ``RuntimeSettingsConfig``
- provider acquisition via ``LLMFactory``
- timing (latency_ms, ttft_ms)
- CancelledError + GeneratorExit → provider.abort() + abort log
- provider errors → error log + re-raise
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import json
from time import monotonic
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.llm.exceptions import LLMInferenceValidationError, LLMProfileNotFoundError
from src.platform.llm.factory import LLMModelInactiveError, LLMModelNotFoundError  # noqa: F401
from src.platform.llm.providers.base import LLMChunk, LLMMessage
from src.platform.llm.repository import get_by_id as _get_model_by_id
from src.platform.llm.repository import get_profile_by_id
from src.platform.llm.schemas import InferenceRequest, InferenceResponse
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_log_participant_fields
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

if TYPE_CHECKING:
    from src.platform.llm.factory import LLMFactory

# Type alias accepted everywhere a log service is needed
_AnyLogService = LogService | NoOpLogService

# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------


def _validate_messages(
    messages: list[LLMMessage],
    settings: RuntimeSettingsConfig,
) -> None:
    """Raise ``LLMInferenceValidationError`` if any size limit is exceeded."""
    if len(messages) > settings.llm_max_messages:
        raise LLMInferenceValidationError(f'Too many messages: {len(messages)} > {settings.llm_max_messages}')
    for msg in messages:
        if len(msg.content) > settings.llm_max_chars_per_message:
            raise LLMInferenceValidationError(
                f'Message content too long: {len(msg.content)} > {settings.llm_max_chars_per_message}'
            )
    total = sum(len(msg.content) for msg in messages)
    if total > settings.llm_max_total_chars:
        raise LLMInferenceValidationError(f'Total message chars too long: {total} > {settings.llm_max_total_chars}')


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------


def _emit_inference_log(
    log_service: _AnyLogService,
    *,
    status: str,
    profile_name: str,
    model_name: str,
    model_id: uuid.UUID,
    profile_id: uuid.UUID,
    tokens_used: int,
    latency_ms: float,
    ttft_ms: float | None,
    error_code: str | None,
    correlation_id: str | None,
    causation_id: str | None,
) -> None:
    """Emit a fire-and-forget inference log entry to aurelion.logs."""
    level = LogLevel.INFO if status in ('success', 'aborted') else LogLevel.ERROR
    payload: dict[str, Any] = {
        'status': status,
        'model': model_name,
        'model_id': str(model_id),
        'execution_profile': profile_name,
        'execution_profile_id': str(profile_id),
        'tokens_used': tokens_used,
        'latency_ms': latency_ms,
    }
    if ttft_ms is not None:
        payload['ttft_ms'] = ttft_ms
    if error_code is not None:
        payload['error_code'] = error_code
    if causation_id is not None:
        payload['causation_id'] = causation_id

    payload = merge_emit_log_participant_fields(
        payload,
        actor_component='llm_inference',
        target_id=str(profile_id),
    )
    log_service.emit_safe(
        level=level,
        message=f'inference {status}: profile={profile_name} model={model_name}',
        component='llm_inference',
        payload=payload,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Core generator
# ---------------------------------------------------------------------------


async def _drive_inference(
    session: AsyncSession,
    factory: LLMFactory,
    *,
    request: InferenceRequest,
    settings: RuntimeSettingsConfig,
    log_service: _AnyLogService,
    correlation_id: str | None,
    causation_id: str | None,
) -> AsyncGenerator[LLMChunk]:
    """Async generator that drives the full inference lifecycle.

    Yields ``LLMChunk`` objects.  Handles timing, abort on disconnect/cancel,
    and logs exactly one entry per invocation.

    Raises
    ------
    LLMProfileNotFoundError
        When the execution profile does not exist.
    LLMModelNotFoundError
        When the model referenced by the profile does not exist.
    LLMModelInactiveError
        When the model exists but is_active=False.
    LLMInferenceValidationError
        When message size limits are exceeded.
    """
    # 1. Resolve profile
    profile = await get_profile_by_id(session, request.execution_profile_id)
    if profile is None:
        raise LLMProfileNotFoundError(f'LLMExecutionProfile not found: {request.execution_profile_id}')

    # 2. Convert messages
    messages = [LLMMessage(role=m.role, content=m.content) for m in request.messages]

    # 3. Validate messages
    _validate_messages(messages, settings)

    # 4. Acquire provider (raises LLMModelNotFoundError / LLMModelInactiveError)
    provider = await factory.get(session, profile.model_id)

    # Fetch model row for metadata (name, default_params)
    model_row = await _get_model_by_id(session, profile.model_id)
    model_name = model_row.name if model_row else str(profile.model_id)

    # 5. Build params
    model_default: dict[str, Any] = dict(model_row.default_params or {}) if model_row else {}
    profile_overrides: dict[str, Any] = dict(profile.param_overrides or {})
    params: dict[str, Any] = {**model_default, **profile_overrides}

    # 6. Stream tokens
    t0 = monotonic()
    ttft_ms: float | None = None
    tokens_used = 0
    latency_ms = 0.0
    status = 'success'
    error_code: str | None = None

    try:
        async for chunk in provider.stream(messages, params):
            if chunk.done:
                latency_ms = (monotonic() - t0) * 1000
                if chunk.tokens_used is not None:
                    tokens_used = chunk.tokens_used
                yield chunk
                break
            # Non-done token chunk
            if ttft_ms is None and chunk.token:
                ttft_ms = (monotonic() - t0) * 1000
            tokens_used += 1 if chunk.token else 0
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        latency_ms = (monotonic() - t0) * 1000
        status = 'aborted'
        try:
            await provider.abort()
        except Exception:  # noqa: BLE001
            pass
        _emit_inference_log(
            log_service,
            status=status,
            profile_name=profile.name,
            model_name=model_name,
            model_id=profile.model_id,
            profile_id=profile.id,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            error_code=None,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        raise
    except Exception as exc:
        latency_ms = (monotonic() - t0) * 1000
        status = 'error'
        error_code = type(exc).__name__
        _emit_inference_log(
            log_service,
            status=status,
            profile_name=profile.name,
            model_name=model_name,
            model_id=profile.model_id,
            profile_id=profile.id,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            error_code=error_code,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        raise
    else:
        _emit_inference_log(
            log_service,
            status=status,
            profile_name=profile.name,
            model_name=model_name,
            model_id=profile.model_id,
            profile_id=profile.id,
            tokens_used=tokens_used,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            error_code=None,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )


# ---------------------------------------------------------------------------
# Public coroutines
# ---------------------------------------------------------------------------


async def run_inference(
    session: AsyncSession,
    factory: LLMFactory,
    *,
    request: InferenceRequest,
    settings: RuntimeSettingsConfig,
    log_service: _AnyLogService,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> InferenceResponse:
    """Run inference and return the full response as a single JSON object.

    Times the whole stream internally so ``latency_ms`` and ``ttft_ms`` in
    the response reflect the actual wall-clock measurements.
    """
    profile = await get_profile_by_id(session, request.execution_profile_id)
    if profile is None:
        raise LLMProfileNotFoundError(f'LLMExecutionProfile not found: {request.execution_profile_id}')

    output_tokens: list[str] = []
    tokens_used = 0
    latency_ms = 0.0
    ttft_ms: float | None = None
    t0 = monotonic()

    async for chunk in _drive_inference(
        session,
        factory,
        request=request,
        settings=settings,
        log_service=log_service,
        correlation_id=correlation_id,
        causation_id=causation_id,
    ):
        if chunk.done:
            latency_ms = (monotonic() - t0) * 1000
            if chunk.output is not None:
                # Provider supplied the fully assembled output in the terminal chunk
                output_tokens = [chunk.output]
            if chunk.tokens_used is not None:
                tokens_used = chunk.tokens_used
        else:
            if ttft_ms is None and chunk.token:
                ttft_ms = (monotonic() - t0) * 1000
            output_tokens.append(chunk.token)

    output = output_tokens[0] if (len(output_tokens) == 1 and output_tokens) else ''.join(output_tokens)

    return InferenceResponse(
        output=output,
        model_id=profile.model_id,
        execution_profile_id=request.execution_profile_id,
        tokens_used=tokens_used,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
    )


async def stream_inference(
    session: AsyncSession,
    factory: LLMFactory,
    *,
    request: InferenceRequest,
    settings: RuntimeSettingsConfig,
    log_service: _AnyLogService,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    is_disconnected: Any = None,
) -> AsyncGenerator[dict[str, Any]]:
    """Yield SSE payload dicts for the streaming inference endpoint.

    Each yielded dict is passed as the ``data`` key of an SSE event dict by
    the route handler.  Format:

    - Token event:  ``{"token": "...", "done": false}``
    - Final event:  ``{"output": "...", "model_id": "...", "tokens_used": N,
                       "latency_ms": N, "ttft_ms": N|null, "done": true}``
    - Error event:  ``{"error": "<ErrorClassName>", "done": true}``
    """
    profile = await get_profile_by_id(session, request.execution_profile_id)
    if profile is None:
        raise LLMProfileNotFoundError(f'LLMExecutionProfile not found: {request.execution_profile_id}')

    output_tokens: list[str] = []
    tokens_used = 0
    latency_ms = 0.0
    ttft_ms: float | None = None
    t0 = monotonic()

    try:
        async for chunk in _drive_inference(
            session,
            factory,
            request=request,
            settings=settings,
            log_service=log_service,
            correlation_id=correlation_id,
            causation_id=causation_id,
        ):
            # Check disconnect between tokens
            if is_disconnected is not None:
                try:
                    disconnected = await is_disconnected()
                except Exception:  # noqa: BLE001
                    disconnected = False
                if disconnected:
                    raise asyncio.CancelledError('client disconnected')

            if chunk.done:
                latency_ms = (monotonic() - t0) * 1000
                if chunk.output is not None:
                    output_tokens = [chunk.output]
                if chunk.tokens_used is not None:
                    tokens_used = chunk.tokens_used
                output = output_tokens[0] if (len(output_tokens) == 1 and output_tokens) else ''.join(output_tokens)
                yield {
                    'data': json.dumps(
                        {
                            'output': output,
                            'model_id': str(profile.model_id),
                            'execution_profile_id': str(request.execution_profile_id),
                            'tokens_used': tokens_used,
                            'latency_ms': latency_ms,
                            'ttft_ms': ttft_ms,
                            'done': True,
                        }
                    )
                }
            else:
                if ttft_ms is None and chunk.token:
                    ttft_ms = (monotonic() - t0) * 1000
                output_tokens.append(chunk.token)
                yield {'data': json.dumps({'token': chunk.token, 'done': False})}

    except (asyncio.CancelledError, GeneratorExit):
        # Cannot yield after CancelledError in an async generator — the error
        # event is omitted on abort; the aborted log entry is enough.
        raise
    except Exception as exc:
        error_code = type(exc).__name__
        yield {'data': json.dumps({'error': error_code, 'done': True})}
        # Do NOT re-raise: provider errors are surfaced via the SSE error event.
        # Re-raising would cause sse-starlette to produce an ExceptionGroup instead.
