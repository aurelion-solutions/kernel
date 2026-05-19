# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Notification template engine — thin Jinja2 wrapper.

Templates live under ``aurelion-kernel/templates/notifications/<channel>/<name>.j2``
and follow a simple two-block convention:

    {% block subject %}<line-1>{% endblock %}
    {% block body %}<rest>{% endblock %}

The wrapper exposes one function ``render(channel, template_name, ctx)``
returning a ``RenderedTemplate`` dataclass with ``subject`` and ``body``.

Templates are immutable at runtime — no template editing surface in this
phase. A missing template is a hard error (the calling pipeline step
fails with a descriptive reason).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound, select_autoescape

_TEMPLATES_ROOT = Path(__file__).resolve().parent.parent.parent.parent / 'templates' / 'notifications'


class TemplateNotFoundError(Exception):
    """Raised when ``render`` is asked for a template that does not exist on disk."""


@dataclass(frozen=True)
class RenderedTemplate:
    subject: str
    body: str


def _env(channel: str) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_ROOT / channel)),
        autoescape=select_autoescape(['html']),  # body is plain text by default
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render(channel: str, template_name: str, ctx: Mapping[str, Any]) -> RenderedTemplate:
    """Render a notification template under ``templates/notifications/<channel>/``.

    Raises :class:`TemplateNotFoundError` if no ``<template_name>.j2`` file
    exists under the channel folder. Missing context keys raise the standard
    Jinja2 ``UndefinedError`` (StrictUndefined is on) so a template that
    references an unprovided ctx key fails loudly instead of silently
    producing empty strings.
    """
    env = _env(channel)
    file_name = f'{template_name}.j2'
    try:
        tmpl = env.get_template(file_name)
    except TemplateNotFound:
        raise TemplateNotFoundError(
            f'Template not found: channel={channel!r} template={template_name!r}; '
            f'expected {_TEMPLATES_ROOT / channel / file_name}'
        ) from None

    context = tmpl.new_context(dict(ctx))

    def _render_block(name: str) -> str:
        block_renderer = tmpl.blocks.get(name)
        if block_renderer is None:
            return ''
        return ''.join(block_renderer(context))

    return RenderedTemplate(
        subject=_render_block('subject').strip(),
        body=_render_block('body').strip(),
    )
