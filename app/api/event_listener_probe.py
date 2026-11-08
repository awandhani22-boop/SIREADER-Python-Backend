"""Owner-side, read-only probe for the Phase 3.3 PostgreSQL LISTEN path.

Run from the Python Backend repository root. The probe reads the existing
SIREADER_API_DATABASE_URL, opens a dedicated autocommit LISTEN connection, and
waits for one allowlisted NOTIFY event. It never prints the DSN or credentials
and does not write to the database.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import psycopg

from app.api.config import api_settings
from app.api.events import ALLOWED_RESOURCES, NOTIFY_CHANNEL


def _database_dsn() -> str:
    value = api_settings.get_api_database_url()
    if value.startswith("postgresql+psycopg://"):
        return value.replace("postgresql+psycopg://", "postgresql://", 1)
    return value


async def _wait_for_notification(wait_seconds: int) -> int:
    try:
        dsn = _database_dsn()
        async with await psycopg.AsyncConnection.connect(
            dsn,
            autocommit=True,
            connect_timeout=5,
            application_name="sireader-observability-listen-probe",
        ) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(f"LISTEN {NOTIFY_CHANNEL}")

            print("LISTEN_READY")
            async for notification in connection.notifies(
                timeout=float(wait_seconds),
                stop_after=1,
            ):
                if notification.channel != NOTIFY_CHANNEL:
                    print("UNEXPECTED_CHANNEL")
                    return 2
                if notification.payload not in ALLOWED_RESOURCES:
                    print("UNEXPECTED_RESOURCE_PAYLOAD")
                    return 2
                print(f"NOTIFICATION_RECEIVED resource={notification.payload}")
                return 0

            print("NO_NOTIFICATION_WITHIN_TIMEOUT")
            return 3
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # psycopg exceptions can contain the DSN; print only the class name.
        print(f"LISTEN_PROBE_FAILED error_type={type(exc).__name__}", file=sys.stderr)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely verify the SIREADER read-only PostgreSQL LISTEN session."
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=90,
        choices=range(5, 301),
        metavar="[5..300]",
        help="Maximum wait for one resource notification (default: 90).",
    )
    args = parser.parse_args()
    return asyncio.run(_wait_for_notification(args.wait_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
