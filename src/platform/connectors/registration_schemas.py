# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from typing import Literal

from pydantic import BaseModel, Field


class OperationDependencyRule(BaseModel):
    """Dependency rule for an operation: specifies which resource must pre-exist with required status."""

    resource: str = Field(
        min_length=1,
        max_length=64,
        description="Resource kind that must pre-exist (e.g. 'account').",
    )
    status: list[str] = Field(
        min_length=1,
        description="Acceptable statuses for the dependency resource (e.g. ['active']).",
    )
    application: str | None = Field(
        default=None,
        description=(
            'Optional application code for cross-application dependencies. '
            'When set, the resource must exist in this application rather than the current one.'
        ),
    )


class AccountDisableCascadeRule(BaseModel):
    """A single revoke item that must be added before account_disable."""

    fact_kind: str = Field(
        min_length=1,
        max_length=64,
        description="Fact kind to revoke (e.g. 'role', 'group', 'entitlement').",
    )


class AccountDisableCascades(BaseModel):
    """Cascade rules for account_disable: revoke items inserted before the disable op."""

    before_disable: list[AccountDisableCascadeRule] = Field(
        default_factory=list,
        description='Fact kinds to revoke before account_disable executes.',
    )


class ConnectorOperationDescriptor(BaseModel):
    """Descriptor for a single operation supported by the connector."""

    kind: str = Field(
        min_length=1,
        max_length=128,
        description="Operation kind identifier (e.g. 'account_create', 'role_grant').",
    )
    dependency_rules: list[OperationDependencyRule] = Field(
        default_factory=list,
        description='Resources that must pre-exist before this operation can execute.',
    )


class AccountStatusTransitions(BaseModel):
    """Allowed account-status transitions for this connector."""

    transitions: list[tuple[str, str]] = Field(
        default_factory=list,
        description=(
            'List of (from_status, to_status) pairs representing valid transitions. '
            "E.g. [('not_exists', 'invited'), ('invited', 'active')]."
        ),
    )


class ConnectorCapabilityDescriptor(BaseModel):
    """Structured capability descriptor sent by the connector on registration."""

    operations: list[ConnectorOperationDescriptor] = Field(
        default_factory=list,
        description='Operations this connector supports.',
    )
    account_status: AccountStatusTransitions = Field(
        default_factory=AccountStatusTransitions,
        description='Allowed account-status transitions.',
    )
    verify_fact_supported: bool = Field(
        default=False,
        description='Whether this connector supports fact verification calls.',
    )
    supported_fact_kinds: list[str] = Field(
        default_factory=list,
        description="Fact kinds (e.g. 'role_grant', 'group_membership') this connector can verify.",
    )
    cascades: AccountDisableCascades = Field(
        default_factory=AccountDisableCascades,
        description='Cascade rules: revoke items to inject before account_disable.',
    )


class ConnectorRegistrationMessage(BaseModel):
    event_type: Literal['connector.registered', 'connector.heartbeat']
    instance_id: str = Field(min_length=1, max_length=255)
    tags: list[str] = Field(default_factory=list)
    descriptor: ConnectorCapabilityDescriptor | None = Field(
        default=None,
        description='Optional capability descriptor. If omitted the stored descriptor is preserved.',
    )
