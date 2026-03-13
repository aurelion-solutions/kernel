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


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def _str_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _parse_binding_keys(raw: str | None) -> list[str]:
    if not raw:
        return ['#']
    return [item.strip() for item in raw.split(',') if item.strip()]


def main() -> None:
    host = _str_env('AURELION_RABBITMQ_HOST', 'localhost')
    port = _int_env('AURELION_RABBITMQ_PORT', 5672)
    username = os.environ.get('AURELION_RABBITMQ_USERNAME') or None
    password = os.environ.get('AURELION_RABBITMQ_PASSWORD') or None

    exchange = _str_env('AURELION_LOGS_EXCHANGE', 'aurelion.logs')
    queue_name = _str_env('AURELION_LOGS_QUEUE', 'aurelion.logs.siem')
    buffer_queue = _str_env('AURELION_LOGS_BUFFER_QUEUE', 'aurelion.logs.buffer')
    binding_keys = _parse_binding_keys(os.environ.get('AURELION_LOGS_BINDINGS'))
    sink_provider = _str_env('AURELION_LOG_SINK_PROVIDER', 'file')

    from src.platform.logs.consumer import run_rabbitmq_consumer
    from src.platform.logs.factory import log_sink_factory
    from src.platform.logs.schemas import LogLevel
    from src.platform.logs.service import LogService

    log_service = LogService(factory=log_sink_factory, provider_name=sink_provider)

    def on_parse_error(raw: dict[str, Any], message: str) -> None:
        log_service.emit_safe(
            'mq_log_consumer.parse_error',
            LogLevel.ERROR,
            message,
            'mq-log-consumer',
            {'raw_preview': str(raw)[:500]},
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
