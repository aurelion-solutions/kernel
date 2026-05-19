# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for the notification template engine."""

from __future__ import annotations

import pytest
from src.engines.notifications.template_engine import (
    TemplateNotFoundError,
    render,
)


def test_render_email_welcome_default() -> None:
    rendered = render('email', 'welcome_employee', {'first_name': 'Ada'})
    assert 'Welcome to Aurelion' in rendered.subject
    assert 'Hi Ada' in rendered.body


def test_render_email_welcome_custom_org() -> None:
    rendered = render('email', 'welcome_employee', {'first_name': 'Ada', 'org_name': 'Acme Corp'})
    assert 'Welcome to Acme Corp' in rendered.subject


def test_render_inapp_leaver_subject_uses_destructive_count() -> None:
    rendered = render(
        'inapp',
        'leaver_confirm_required',
        {'case_id': 'case-1', 'destructive_count': 5},
    )
    assert '5 items' in rendered.subject
    assert 'case-1' in rendered.body


def test_render_unknown_template_raises() -> None:
    with pytest.raises(TemplateNotFoundError):
        render('email', 'no_such_template_anywhere', {})


def test_render_strict_undefined_surfaces_missing_var() -> None:
    """A template that references a missing ctx key must fail rather than render ''."""
    # webhook/case_completed.j2 requires case_id, subject_ref, from_state, to_state
    with pytest.raises(Exception) as exc_info:
        render('webhook', 'case_completed', {'case_id': 'x'})
    assert 'undefined' in str(exc_info.value).lower() or 'subject_ref' in str(exc_info.value)
