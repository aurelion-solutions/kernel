# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine actions for the SoD evaluator slice.

Actions registered here wrap the existing SodEvaluatorService without touching
service.py, routes.py, or any other existing file. Registration happens at
import time via @register_action decorator side effects.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from src.engines.policy_assessment.policy_types.sod.schemas import (
    SodEvaluateRequest,
    SodViolationResponse,
    SodWhatIfRequest,
)
from src.engines.policy_assessment.policy_types.sod.service import SodEvaluatorService
from src.platform.orchestrator.registry import ActionContext, register_action


class SodEvaluateResult(BaseModel):
    """Result wrapper for sod.evaluate and sod.what_if actions."""

    model_config = ConfigDict(frozen=True)

    violations: list[SodViolationResponse]


@register_action(  # type: ignore[arg-type]
    engine='policy_assessment.sod',
    action='evaluate',
    args_schema=SodEvaluateRequest,
    result_schema=SodEvaluateResult,
    idempotent=True,
)
async def sod_evaluate(args: SodEvaluateRequest, ctx: ActionContext) -> SodEvaluateResult:
    """Evaluate all active SoD rules for a subject at a point in time."""
    at = args.at if args.at is not None else datetime.now(UTC)
    service = SodEvaluatorService(ctx.session)
    violations = await service.evaluate_subject(subject_id=args.subject_id, at=at)
    return SodEvaluateResult(violations=[SodViolationResponse.from_violation(v) for v in violations])


@register_action(  # type: ignore[arg-type]
    engine='policy_assessment.sod',
    action='what_if',
    args_schema=SodWhatIfRequest,
    result_schema=SodEvaluateResult,
    idempotent=True,
)
async def sod_what_if(args: SodWhatIfRequest, ctx: ActionContext) -> SodEvaluateResult:
    """Evaluate SoD rules for a subject with synthetic capability overrides."""
    at = args.at if args.at is not None else datetime.now(UTC)
    service = SodEvaluatorService(ctx.session)
    violations = await service.what_if_subject(
        subject_id=args.subject_id,
        at=at,
        capability_overrides=args.capability_overrides,
    )
    return SodEvaluateResult(violations=[SodViolationResponse.from_violation(v) for v in violations])
