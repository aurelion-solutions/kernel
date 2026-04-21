# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EventSink factory for provider resolution by name."""

from collections.abc import Callable

from src.platform.events.interface import EventSink
from src.platform.events.service import _NoOpEventSink


class UnsupportedProviderError(Exception):
    """Raised when the requested event sink provider is not registered."""


class EventSinkFactory:
    """Resolves :class:`EventSink` by provider name. Uses lazy instantiation.

    The ``'mq'`` provider is NOT registered at import time because
    :class:`~src.platform.events.providers.mq.RabbitMQEventSink` requires a
    shared :class:`~src.core.mq.async_publisher.AsyncRabbitMQPublisher` that is
    only available after application startup.  The ``'mq'`` factory is wired in
    the FastAPI lifespan (``src/runtimes/platform_api/main.py``) via
    :meth:`register`.
    """

    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], EventSink]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register('noop', lambda: _NoOpEventSink())

    def register(
        self,
        name: str,
        provider_factory: Callable[[], EventSink],
    ) -> None:
        """Register a provider factory. Called for each :meth:`get`."""
        self._providers[name] = provider_factory

    def list_names(self) -> list[str]:
        """Return sorted list of registered provider names."""
        return sorted(self._providers.keys())

    def get(self, provider_name: str) -> EventSink:
        """Return a new :class:`EventSink` instance for the given provider."""
        if provider_name not in self._providers:
            raise UnsupportedProviderError(f'Unsupported event sink provider: {provider_name!r}')
        return self._providers[provider_name]()


event_sink_factory = EventSinkFactory()
