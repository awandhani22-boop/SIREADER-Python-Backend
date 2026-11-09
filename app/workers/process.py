"""CLI entry point for an explicitly enabled generic worker process.

Example:
    python -m app.workers.process --worker haiku

WORKER_RUNTIME_ENABLED must be set explicitly. This process never starts from
the Telegram bot, the main application, or the read-only API automatically.
"""
from __future__ import annotations

import argparse
import logging

from app.workers.registry import build_default_registry
from app.workers.runtime import run_worker_forever


def main() -> None:
    parser = argparse.ArgumentParser(description="SIREADER generic workforce worker")
    parser.add_argument(
        "--worker",
        required=True,
        choices=tuple(build_default_registry()),
        help="Stable worker ID from the backend registry.",
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_worker_forever(args.worker, poll_interval_seconds=args.poll_interval)


if __name__ == "__main__":
    main()
