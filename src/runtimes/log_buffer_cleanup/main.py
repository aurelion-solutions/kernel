# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""
Delete ``log_event_buffer`` rows whose event ``timestamp`` is older than the configured TTL.

Standalone process; not coupled to the MQ buffer consumer.
"""

import asyncio

from dotenv import load_dotenv

load_dotenv()

# ruff: noqa: E402
from src.platform.secrets.factory import register_default_providers

register_default_providers()
from src.core.db.session import get_session_factory
from src.platform.logs.buffer_cleanup import run_log_buffer_cleanup_once
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService
from src.platform.runtime_settings.service import RuntimeSettingsService


def main() -> None:
    log_service = LogService(sink=log_sink_factory.get('file'))

    async def _run() -> tuple[int, int]:
        session_factory = get_session_factory()

        # Load runtime settings to get log_buffer_retention_seconds
        async with session_factory() as session:
            rt_service = RuntimeSettingsService(session, NoOpLogService())
            runtime = await rt_service.load()

        retention_secs = runtime.log_buffer_retention_seconds

        deleted_rows = await run_log_buffer_cleanup_once(
            session_factory,
            retention_seconds=retention_secs,
        )
        return retention_secs, deleted_rows

    retention, deleted = asyncio.run(_run())

    log_service.emit_safe(
        level=LogLevel.INFO,
        message=f'log_buffer_cleanup: retention_seconds={retention} deleted_rows={deleted}',
        component='log.buffer.cleanup',
        payload={
            'initiator_type': 'system',
            'initiator_id': 'platform',
            'actor_type': 'system',
            'actor_id': 'log.buffer.cleanup',
            'target_type': 'system',
            'target_id': 'log_event_buffer',
            'deleted_rows': deleted,
        },
    )


if __name__ == '__main__':
    main()
