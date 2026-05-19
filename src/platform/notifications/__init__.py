# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Aurelion platform notifications subsystem.

Four sibling channels (Phase 20 K-C/D/E/F), each following the structure
of ``platform/secrets/`` and ``platform/storage/``:

- ``email``    — outbound email
- ``sms``      — outbound SMS
- ``webhook``  — outbound HTTP webhook
- ``inapp``    — in-app notifications, persisted by emitting an MQ event
                  whose routing key carries the product segment
                  (e.g. ``notifications.inapp_journey.dispatched``).

Each channel ships:

- ``interface.py`` — typed Protocol ``<Channel>Sender`` with one method
  ``send(message)`` that returns a ``<Channel>SendResult``.
- ``factory.py`` — ``<channel>_sender_factory`` resolving providers by name
  via ``AURELION_NOTIFICATIONS_<CHANNEL>_PROVIDER`` (defaults to ``file``).
- ``providers/file.py`` — mandatory default that writes outgoing messages
  to a local file. Configurable via
  ``AURELION_NOTIFICATIONS_<CHANNEL>_FILE_PATH``.
- ``providers/<real>.py`` — at least one real provider per channel.
- ``tests/`` — unit tests for ``file`` provider and the factory.

The kernel engine wrapper ``engines/notifications/`` (Phase 20 K-I) calls
these subsystems through the ``Sender`` protocol; pipeline cartridges call
the engine actions, not the subsystem directly.
"""
