# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Subject Pydantic schemas."""

import uuid

from pydantic import ValidationError
import pytest
from src.inventory.subjects.schemas import SubjectAttributeCreate, SubjectCreate, SubjectPatch


def _emp_id() -> uuid.UUID:
    return uuid.uuid4()


def _nhi_id() -> uuid.UUID:
    return uuid.uuid4()


def _cust_id() -> uuid.UUID:
    return uuid.uuid4()


class TestSubjectCreateValid:
    def test_employee_kind(self) -> None:
        pid = _emp_id()
        obj = SubjectCreate(
            external_id='emp-001',
            kind='employee',
            principal_employee_id=pid,
            status='active',
        )
        assert obj.kind.value == 'employee'
        assert obj.nhi_kind is None

    def test_nhi_kind(self) -> None:
        pid = _nhi_id()
        obj = SubjectCreate(
            external_id='nhi-001',
            kind='nhi',
            nhi_kind='api_key',
            principal_nhi_id=pid,
            status='active',
        )
        assert obj.nhi_kind is not None
        assert obj.nhi_kind.value == 'api_key'

    def test_customer_kind(self) -> None:
        pid = _cust_id()
        obj = SubjectCreate(
            external_id='cust-001',
            kind='customer',
            principal_customer_id=pid,
            status='registered',
        )
        assert obj.status == 'registered'


class TestSubjectCreateInvalidStatus:
    def test_employee_rejects_nhi_status(self) -> None:
        with pytest.raises(ValidationError, match='not valid for kind'):
            SubjectCreate(
                external_id='x',
                kind='employee',
                principal_employee_id=_emp_id(),
                status='expired',
            )

    def test_nhi_rejects_employee_status(self) -> None:
        with pytest.raises(ValidationError, match='not valid for kind'):
            SubjectCreate(
                external_id='x',
                kind='nhi',
                nhi_kind='bot',
                principal_nhi_id=_nhi_id(),
                status='hired',
            )

    def test_customer_rejects_nhi_status(self) -> None:
        with pytest.raises(ValidationError, match='not valid for kind'):
            SubjectCreate(
                external_id='x',
                kind='customer',
                principal_customer_id=_cust_id(),
                status='expired',
            )


class TestSubjectCreateNhiKindConsistency:
    def test_nhi_without_nhi_kind_fails(self) -> None:
        with pytest.raises(ValidationError, match='nhi_kind is required'):
            SubjectCreate(
                external_id='x',
                kind='nhi',
                principal_nhi_id=_nhi_id(),
                status='active',
            )

    def test_employee_with_nhi_kind_fails(self) -> None:
        with pytest.raises(ValidationError, match='nhi_kind must be null'):
            SubjectCreate(
                external_id='x',
                kind='employee',
                nhi_kind='bot',
                principal_employee_id=_emp_id(),
                status='active',
            )


class TestSubjectCreatePrincipalExclusivity:
    def test_two_principals_fails(self) -> None:
        with pytest.raises(ValidationError, match='Exactly one'):
            SubjectCreate(
                external_id='x',
                kind='employee',
                principal_employee_id=_emp_id(),
                principal_nhi_id=_nhi_id(),
                status='active',
            )

    def test_zero_principals_fails(self) -> None:
        with pytest.raises(ValidationError, match='Exactly one'):
            SubjectCreate(
                external_id='x',
                kind='employee',
                status='active',
            )

    def test_wrong_principal_for_kind_fails(self) -> None:
        with pytest.raises(ValidationError, match='principal_employee_id must be set'):
            SubjectCreate(
                external_id='x',
                kind='employee',
                principal_nhi_id=_nhi_id(),
                status='active',
            )


class TestSubjectPatch:
    def test_patch_only_has_status(self) -> None:
        p = SubjectPatch(status='active')
        assert p.status == 'active'

    def test_patch_empty_is_valid(self) -> None:
        p = SubjectPatch()
        assert p.status is None

    def test_patch_has_no_kind_field(self) -> None:
        assert 'kind' not in SubjectPatch.model_fields
        assert 'nhi_kind' not in SubjectPatch.model_fields
        assert 'principal_employee_id' not in SubjectPatch.model_fields
        assert set(SubjectPatch.model_fields.keys()) == {'status'}


class TestSubjectAttributeCreate:
    def test_valid_create(self) -> None:
        obj = SubjectAttributeCreate(key='cost_center', value='cc-01')
        assert obj.key == 'cost_center'
        assert obj.value == 'cc-01'

    def test_rejects_empty_key(self) -> None:
        with pytest.raises(ValidationError):
            SubjectAttributeCreate(key='', value='v')

    def test_rejects_empty_value(self) -> None:
        with pytest.raises(ValidationError):
            SubjectAttributeCreate(key='k', value='')

    def test_rejects_oversized_key_256(self) -> None:
        with pytest.raises(ValidationError):
            SubjectAttributeCreate(key='x' * 256, value='v')

    def test_rejects_oversized_value_1025(self) -> None:
        with pytest.raises(ValidationError):
            SubjectAttributeCreate(key='k', value='x' * 1025)

    def test_accepts_max_key_255(self) -> None:
        obj = SubjectAttributeCreate(key='x' * 255, value='v')
        assert len(obj.key) == 255

    def test_accepts_max_value_1024(self) -> None:
        obj = SubjectAttributeCreate(key='k', value='x' * 1024)
        assert len(obj.value) == 1024
