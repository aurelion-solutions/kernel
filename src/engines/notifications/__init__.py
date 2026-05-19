# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Notifications engine — pipeline-callable wrapper around the four
``platform/notifications/<channel>/`` subsystems (Phase 20 K-I).

Four actions registered at import time via ``@register_action``:

- ``notifications.send_email`` → email channel
- ``notifications.send_sms``    → sms channel
- ``notifications.send_webhook`` → webhook channel
- ``notifications.send_inapp``  → inapp channel

All four are ``idempotent=False`` — the orchestrator must not assume a
``send_*`` can be replayed safely. Pipeline authors who do want at-most-once
should wrap the action in a guard step that checks a domain-side persisted
record before issuing the send.
"""

from src.engines.notifications import actions as _actions  # noqa: F401 — register actions at import
