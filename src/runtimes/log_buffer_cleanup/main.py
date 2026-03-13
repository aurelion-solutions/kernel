# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""
Delete ``log_event_buffer`` rows whose event ``timestamp`` is older than the configured TTL.

Standalone process; not coupled to the MQ buffer consumer.
"""

import asyncio
import sys

from dotenv import load_dotenv
from src.core.config import settings
from src.core.db.session import SessionLocal
from src.platform.logs.buffer_cleanup import run_log_buffer_cleanup_once

load_dotenv()


def main() -> None:
    retention = settings.log_buffer_retention_seconds
    deleted = asyncio.run(
        run_log_buffer_cleanup_once(
            SessionLocal,
            retention_seconds=retention,
        ),
    )
    print(
        f'log_buffer_cleanup: retention_seconds={retention} deleted_rows={deleted}',
        file=sys.stderr,
    )


if __name__ == '__main__':
    main()
