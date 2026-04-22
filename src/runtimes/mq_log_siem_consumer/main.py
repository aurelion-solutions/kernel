# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""
Standalone service: consume MQ log events and emit them into the final sink.
"""

import os
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _str_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _parse_binding_keys(raw: str | None) -> list[str]:
    if not raw:
        return ['#']
    return [item.strip() for item in raw.split(',') if item.strip()]


def main() -> None:
    from src.core.config import settings

    host = settings.rabbitmq_host
    port = settings.rabbitmq_port
    username: str | None = settings.rabbitmq_username
    password: str | None = settings.rabbitmq_password

    exchange = settings.rabbitmq_logs_exchange
    queue_name = _str_env('AURELION_LOGS_QUEUE', 'aurelion.logs.siem')
    buffer_queue = _str_env('AURELION_LOGS_BUFFER_QUEUE', 'aurelion.logs.buffer')
    binding_keys = _parse_binding_keys(os.environ.get('AURELION_LOGS_BINDINGS'))
    sink_provider = _str_env('AURELION_LOG_SINK_PROVIDER', 'file')

    from src.platform.logs.consumer import run_rabbitmq_consumer
    from src.platform.logs.factory import log_sink_factory
    from src.platform.logs.schemas import LogLevel
    from src.platform.logs.service import LogService

    log_service = LogService(sink=log_sink_factory.get(sink_provider))

    def on_parse_error(raw: dict[str, Any], message: str) -> None:
        # NOTE: kwarg-shape refactor (Step 23 Phase 10) — NOT a migration to aurelion.events bus.
        log_service.emit_safe(
            level=LogLevel.ERROR,
            message=message,
            component='mq-log-consumer',
            payload={'raw_preview': str(raw)[:500]},
        )

    companion = (buffer_queue,) if buffer_queue != queue_name else ()

    print(
        f'Starting MQ SIEM log consumer: {host}:{port} exchange={exchange} queue={queue_name}'
        + (f' companion_queues={companion}' if companion else ''),
        file=sys.stderr,
    )

    run_rabbitmq_consumer(
        host=host,
        port=port,
        exchange=exchange,
        queue_name=queue_name,
        binding_keys=binding_keys,
        log_service=log_service,
        username=username,
        password=password,
        on_parse_error=on_parse_error,
        companion_queues=companion,
    )


if __name__ == '__main__':
    main()
