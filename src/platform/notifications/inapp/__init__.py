# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""In-app notification channel (Phase 20 K-F).

Unlike email/SMS/webhook, the in-app channel does not have an external
recipient — it surfaces inside a product UI (Aurelion Journey for Phase 20).
The provider emits an MQ event on the routing key carried in the message
(``routing_key`` field), so the product-side MQ subscriber (J-G in Journey)
picks it up and persists a ``JourneyNotification`` row.

Kernel never knows about the product DB — it just emits the event. The
routing-key naming convention is by product: ``notifications.inapp_journey.*``
for Journey, ``notifications.inapp_pulse.*`` for Pulse, etc. The cartridge
author chooses the key.
"""
