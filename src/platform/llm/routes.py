# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""LLM CRUD routes — models, execution profiles, and inference.

Endpoints — models
------------------
GET    /api/v0/llm/models              → list[LLMModelRead]
POST   /api/v0/llm/models              → 201 LLMModelRead
GET    /api/v0/llm/models/{model_id}   → LLMModelRead
PATCH  /api/v0/llm/models/{model_id}   → LLMModelRead
DELETE /api/v0/llm/models/{model_id}   → 204

Endpoints — execution profiles
-------------------------------
GET    /api/v0/llm/execution-profiles              → list[LLMExecutionProfileRead]
POST   /api/v0/llm/execution-profiles              → 201 LLMExecutionProfileRead
GET    /api/v0/llm/execution-profiles/{id}         → LLMExecutionProfileRead
PATCH  /api/v0/llm/execution-profiles/{id}         → LLMExecutionProfileRead
DELETE /api/v0/llm/execution-profiles/{id}         → 204

Endpoints — inference
---------------------
POST /api/v0/inference        → 200 InferenceResponse (JSON)
POST /api/v0/inference/stream → 200 text/event-stream (SSE)

Handler discipline: thin — validate input via FastAPI/Pydantic, call service,
translate domain errors to HTTP, commit transaction, return response.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse
from src.core.context import current_correlation_id
from src.core.db.deps import get_db
from src.platform.llm.deps import get_llm_factory, get_runtime_settings
from src.platform.llm.exceptions import (
    LLMInferenceValidationError,
    LLMModelInvalidConfigError,
    LLMModelNameAlreadyExistsError,
    LLMModelNotFoundError,
    LLMProfileInvalidConfigError,
    LLMProfileNameAlreadyExistsError,
    LLMProfileNotFoundError,
)
from src.platform.llm.factory import LLMFactory, LLMModelInactiveError
from src.platform.llm.factory import LLMModelNotFoundError as LLMFactoryModelNotFoundError
from src.platform.llm.inference_service import run_inference, stream_inference
from src.platform.llm.schemas import (
    InferenceRequest,
    InferenceResponse,
    LLMExecutionProfileCreate,
    LLMExecutionProfileRead,
    LLMExecutionProfileUpdate,
    LLMModelCreate,
    LLMModelRead,
    LLMModelUpdate,
)
from src.platform.llm.service import (
    create_llm_model,
    create_llm_profile,
    delete_llm_model,
    delete_llm_profile,
    get_llm_model,
    get_llm_profile,
    list_llm_models,
    list_llm_profiles,
    update_llm_model,
    update_llm_profile,
)
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

models_router = APIRouter(prefix='/llm/models', tags=['llm-models'])
profiles_router = APIRouter(prefix='/llm/execution-profiles', tags=['llm-execution-profiles'])
inference_router = APIRouter(prefix='/inference', tags=['llm-inference'])

DependsSession = Depends(get_db)
DependsFactory = Depends(get_llm_factory)
DependsLogService = Depends(get_log_service)
DependsRuntimeSettings = Depends(get_runtime_settings)


# ---------------------------------------------------------------------------
# LLMModel handlers
# ---------------------------------------------------------------------------


@models_router.get('', response_model=list[LLMModelRead])
async def list_(
    session: AsyncSession = DependsSession,
) -> list[LLMModelRead]:
    models = await list_llm_models(session)
    return [LLMModelRead.model_validate(m) for m in models]


@models_router.post('', response_model=LLMModelRead, status_code=201)
async def create(
    request: LLMModelCreate,
    session: AsyncSession = DependsSession,
    log_service: LogService = DependsLogService,
) -> LLMModelRead:
    try:
        model = await create_llm_model(session, request, log_service=log_service)
    except LLMModelNameAlreadyExistsError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except LLMModelInvalidConfigError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    await session.commit()
    return LLMModelRead.model_validate(model)


@models_router.get('/{model_id}', response_model=LLMModelRead)
async def get_one(
    model_id: uuid.UUID,
    session: AsyncSession = DependsSession,
) -> LLMModelRead:
    model = await get_llm_model(session, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail='LLMModel not found')
    return LLMModelRead.model_validate(model)


@models_router.patch('/{model_id}', response_model=LLMModelRead)
async def patch_(
    model_id: uuid.UUID,
    request: LLMModelUpdate,
    session: AsyncSession = DependsSession,
    factory: LLMFactory = DependsFactory,
    log_service: LogService = DependsLogService,
) -> LLMModelRead:
    try:
        model = await update_llm_model(
            session,
            model_id,
            request,
            factory=factory,
            log_service=log_service,
        )
    except LLMModelNotFoundError as err:
        raise HTTPException(status_code=404, detail='LLMModel not found') from err
    except LLMModelNameAlreadyExistsError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except LLMModelInvalidConfigError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    await session.commit()
    return LLMModelRead.model_validate(model)


@models_router.delete('/{model_id}', status_code=204)
async def delete(
    model_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    factory: LLMFactory = DependsFactory,
    log_service: LogService = DependsLogService,
) -> None:
    try:
        await delete_llm_model(session, model_id, factory=factory, log_service=log_service)
    except LLMModelNotFoundError as err:
        raise HTTPException(status_code=404, detail='LLMModel not found') from err
    except LLMModelInvalidConfigError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    await session.commit()


# ---------------------------------------------------------------------------
# LLMExecutionProfile handlers
# ---------------------------------------------------------------------------


@profiles_router.get('', response_model=list[LLMExecutionProfileRead])
async def list_profiles_(
    session: AsyncSession = DependsSession,
) -> list[LLMExecutionProfileRead]:
    profiles = await list_llm_profiles(session)
    return [LLMExecutionProfileRead.model_validate(p) for p in profiles]


@profiles_router.post('', response_model=LLMExecutionProfileRead, status_code=201)
async def create_profile(
    request: LLMExecutionProfileCreate,
    session: AsyncSession = DependsSession,
    log_service: LogService = DependsLogService,
) -> LLMExecutionProfileRead:
    try:
        profile = await create_llm_profile(session, request, log_service=log_service)
    except LLMProfileNameAlreadyExistsError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except LLMProfileInvalidConfigError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    await session.commit()
    return LLMExecutionProfileRead.model_validate(profile)


@profiles_router.get('/{profile_id}', response_model=LLMExecutionProfileRead)
async def get_one_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = DependsSession,
) -> LLMExecutionProfileRead:
    profile = await get_llm_profile(session, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail='LLMExecutionProfile not found')
    return LLMExecutionProfileRead.model_validate(profile)


@profiles_router.patch('/{profile_id}', response_model=LLMExecutionProfileRead)
async def patch_profile(
    profile_id: uuid.UUID,
    request: LLMExecutionProfileUpdate,
    session: AsyncSession = DependsSession,
    log_service: LogService = DependsLogService,
) -> LLMExecutionProfileRead:
    try:
        profile = await update_llm_profile(
            session,
            profile_id,
            request,
            log_service=log_service,
        )
    except LLMProfileNotFoundError as err:
        raise HTTPException(status_code=404, detail='LLMExecutionProfile not found') from err
    except LLMProfileNameAlreadyExistsError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except LLMProfileInvalidConfigError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    await session.commit()
    return LLMExecutionProfileRead.model_validate(profile)


@profiles_router.delete('/{profile_id}', status_code=204)
async def delete_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    log_service: LogService = DependsLogService,
) -> None:
    try:
        await delete_llm_profile(session, profile_id, log_service=log_service)
    except LLMProfileNotFoundError as err:
        raise HTTPException(status_code=404, detail='LLMExecutionProfile not found') from err
    await session.commit()


# ---------------------------------------------------------------------------
# Inference handlers
# ---------------------------------------------------------------------------


@inference_router.post('', response_model=InferenceResponse)
async def post_inference(
    request_body: InferenceRequest,
    request: Request,
    session: AsyncSession = DependsSession,
    factory: LLMFactory = DependsFactory,
    log_service: LogService = DependsLogService,
    settings: RuntimeSettingsConfig = DependsRuntimeSettings,
    x_causation_id: str | None = Header(default=None, alias='X-Causation-ID'),
) -> InferenceResponse:
    """Run inference and return the full assembled response as JSON."""
    correlation_id = current_correlation_id()
    try:
        return await run_inference(
            session,
            factory,
            request=request_body,
            settings=settings,
            log_service=log_service,
            correlation_id=correlation_id,
            causation_id=x_causation_id,
        )
    except LLMProfileNotFoundError as err:
        raise HTTPException(status_code=404, detail='LLMExecutionProfile not found') from err
    except (LLMFactoryModelNotFoundError, LLMModelNotFoundError) as err:
        raise HTTPException(status_code=422, detail='LLM model not found') from err
    except LLMModelInactiveError as err:
        raise HTTPException(status_code=422, detail='LLM model is inactive') from err
    except LLMInferenceValidationError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


@inference_router.post('/stream')
async def post_inference_stream(
    request_body: InferenceRequest,
    request: Request,
    session: AsyncSession = DependsSession,
    factory: LLMFactory = DependsFactory,
    log_service: LogService = DependsLogService,
    settings: RuntimeSettingsConfig = DependsRuntimeSettings,
    x_causation_id: str | None = Header(default=None, alias='X-Causation-ID'),
) -> EventSourceResponse:
    """Stream inference tokens as Server-Sent Events."""
    correlation_id = current_correlation_id()

    try:
        generator = stream_inference(
            session,
            factory,
            request=request_body,
            settings=settings,
            log_service=log_service,
            correlation_id=correlation_id,
            causation_id=x_causation_id,
            is_disconnected=request.is_disconnected,
        )
    except LLMProfileNotFoundError as err:
        raise HTTPException(status_code=404, detail='LLMExecutionProfile not found') from err
    except (LLMFactoryModelNotFoundError, LLMModelNotFoundError) as err:
        raise HTTPException(status_code=422, detail='LLM model not found') from err
    except LLMModelInactiveError as err:
        raise HTTPException(status_code=422, detail='LLM model is inactive') from err
    except LLMInferenceValidationError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err

    return EventSourceResponse(generator)
