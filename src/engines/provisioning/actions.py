# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine actions for the provisioning slice (Phase 18 Step 9e).

Two actions registered at import time via @register_action:

  - (provisioning, create_account) → engines.provisioning.create_account.create_account
  - (provisioning, delete_account) → engines.provisioning.delete_account.delete_account

idempotent=True contract rationale:
  The action declares ``idempotent=True`` on the orchestrator-runner contract.
  Idempotency is delegated to the target connector. Connector authors MUST
  guarantee that ``create_account(username=X)`` and ``delete_account(username=X)``
  are safe to retry. Aurelion does not retry around ``connector.invoke``.

Library-module discipline: no ``get_settings()``, no ``load_dotenv()``,
no ``register_default_providers()`` at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from src.engines.provisioning.create_account import create_account
from src.engines.provisioning.delete_account import delete_account
from src.engines.provisioning.schemas import AccountCreateRequest
from src.platform.connectors.factory import get_process_connector_client
from src.platform.orchestrator.registry import ActionContext, register_action

if TYPE_CHECKING:
    from src.platform.connectors.client import ConnectorClient

# ---------------------------------------------------------------------------
# Args / Result schemas
# ---------------------------------------------------------------------------


class CreateAccountArgs(BaseModel):
    """Args for provisioning.create_account action."""

    model_config = ConfigDict(extra='forbid')

    application_id: UUID
    username: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)


class CreateAccountResult(BaseModel):
    """Result envelope for provisioning.create_account action."""

    model_config = ConfigDict(extra='forbid')

    username: str
    email: str | None
    status: str


class DeleteAccountArgs(BaseModel):
    """Args for provisioning.delete_account action."""

    model_config = ConfigDict(extra='forbid')

    application_id: UUID
    username: str = Field(min_length=1, max_length=255)


class DeleteAccountResult(BaseModel):
    """Result envelope for provisioning.delete_account action."""

    model_config = ConfigDict(extra='forbid')

    status: str


# ---------------------------------------------------------------------------
# Service construction helper
# ---------------------------------------------------------------------------


def _build_connector_client() -> ConnectorClient:
    """Wrap get_process_connector_client() for stable patch target in tests."""
    return get_process_connector_client()


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


@register_action(  # type: ignore[arg-type]
    engine='provisioning',
    action='create_account',
    args_schema=CreateAccountArgs,
    result_schema=CreateAccountResult,
    idempotent=True,
)
async def create_account_action(args: CreateAccountArgs, ctx: ActionContext) -> CreateAccountResult:
    """Delegate to provisioning.create_account and return a result envelope."""
    connector = _build_connector_client()
    result_dict = await create_account(
        ctx.session,
        args.application_id,
        AccountCreateRequest(username=args.username, email=args.email),
        connector,
        log_service=ctx.log_service,
    )
    return CreateAccountResult.model_validate(result_dict)


@register_action(  # type: ignore[arg-type]
    engine='provisioning',
    action='delete_account',
    args_schema=DeleteAccountArgs,
    result_schema=DeleteAccountResult,
    idempotent=True,
)
async def delete_account_action(args: DeleteAccountArgs, ctx: ActionContext) -> DeleteAccountResult:
    """Delegate to provisioning.delete_account and synthesise accepted status."""
    connector = _build_connector_client()
    await delete_account(
        ctx.session,
        args.application_id,
        args.username,
        connector,
        log_service=ctx.log_service,
    )
    return DeleteAccountResult(status='accepted')
