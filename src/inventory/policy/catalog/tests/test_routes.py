# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Route tests for the policy catalog endpoint.

Strategy: dependency-override the service with a stub, no live DB.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.assessment.findings.models import FindingKind
from src.inventory.policy.catalog.deps import get_policy_catalog_service
from src.inventory.policy.catalog.routes import router as policy_catalog_router
from src.inventory.policy.catalog.schemas import (
    PolicyCatalogItem,
    PolicyCatalogResponse,
    PolicyFindingsFilter,
)
from src.inventory.policy.catalog.service import PolicyCatalogService
from src.inventory.policy.enums import (
    AssessmentStrategy,
    DefinitionSource,
    PolicyStatus,
    PolicyType,
)


def _make_app(payload: PolicyCatalogResponse) -> FastAPI:
    noop_session = MagicMock(spec=AsyncSession)

    async def override_session() -> AsyncGenerator[AsyncSession]:
        yield noop_session

    app = FastAPI()
    app.include_router(policy_catalog_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_session

    stub = MagicMock(spec=PolicyCatalogService)

    async def _get_catalog(_session: AsyncSession) -> PolicyCatalogResponse:
        return payload

    stub.get_catalog = _get_catalog
    app.dependency_overrides[get_policy_catalog_service] = lambda: stub
    return app


@pytest.mark.asyncio
async def test_get_policy_catalog_returns_unified_payload() -> None:
    payload = PolicyCatalogResponse(
        items=[
            PolicyCatalogItem(
                id='sod.rule.SOD-001',
                name='Cashier vs Approver',
                description='Cashier and approver capabilities must not overlap.',
                policy_type=PolicyType.SOD,
                definition_source=DefinitionSource.DB,
                assessment_strategy=AssessmentStrategy.DETERMINISTIC,
                status=PolicyStatus.ACTIVE,
                version=None,
                open_findings_count=2,
                findings_filter=PolicyFindingsFilter(kind=None, rule_id=42),
            ),
            PolicyCatalogItem(
                id='lens.access_risk.orphaned_access',
                name='Orphaned Access',
                description='Accounts without owners.',
                policy_type=PolicyType.ACCESS_RISK,
                definition_source=DefinitionSource.FILE,
                assessment_strategy=AssessmentStrategy.DETERMINISTIC,
                status=PolicyStatus.AVAILABLE,
                version=1,
                open_findings_count=3,
                findings_filter=PolicyFindingsFilter(kind=FindingKind.orphan_access, rule_id=None),
            ),
        ]
    )
    app = _make_app(payload)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        resp = await client.get('/api/v0/policies/catalog')

    assert resp.status_code == 200
    body = resp.json()
    assert {i['id'] for i in body['items']} == {
        'sod.rule.SOD-001',
        'lens.access_risk.orphaned_access',
    }
    sod = next(i for i in body['items'] if i['id'] == 'sod.rule.SOD-001')
    assert sod['policy_type'] == 'sod'
    assert sod['definition_source'] == 'db'
    assert sod['assessment_strategy'] == 'deterministic'
    assert sod['status'] == 'active'
    assert sod['version'] is None

    assert sod['open_findings_count'] == 2
    assert sod['findings_filter'] == {'kind': None, 'rule_id': 42}

    cart = next(i for i in body['items'] if i['id'] == 'lens.access_risk.orphaned_access')
    assert cart['definition_source'] == 'file'
    assert cart['status'] == 'available'
    assert cart['version'] == 1
    assert cart['open_findings_count'] == 3
    assert cart['findings_filter'] == {'kind': 'orphan_access', 'rule_id': None}
