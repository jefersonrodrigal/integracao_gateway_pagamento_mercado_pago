"""Worker durável: fila de webhooks no PostgreSQL, reconciliação e retenção.

Uso:
    python -m app.worker          # loop contínuo
    python -m app.worker --once   # um ciclo (cron/diagnóstico)

Vários workers podem rodar em paralelo: eventos são reservados com SKIP LOCKED.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import time

from app.container import Container
from app.core.config import ConfigurationError, get_settings
from app.core.logging import configure_logging, log_fields

logger = logging.getLogger("app.worker")
RETENTION_INTERVAL_SECONDS = 3600


async def run_cycle(container: Container) -> dict[str, int]:
    batch = container.settings.worker_batch_size
    stats = {"events": 0, "payments": 0, "refunds": 0}
    stats["events"] = await container.webhooks.process_due(batch)
    stats["payments"] = await container.reconciliation.reconcile_payments(batch)
    stats["refunds"] = await container.reconciliation.reconcile_refunds(batch)
    return stats


async def main(once: bool = False) -> None:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        app_name=f"{settings.app_name}-worker",
        environment=settings.app_env,
    )
    container = Container.build(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows não suporta add_signal_handler
            loop.add_signal_handler(sig, stop.set)

    last_retention = 0.0
    logger.info("worker_started")
    try:
        while not stop.is_set():
            try:
                stats = await run_cycle(container)
                if time.monotonic() - last_retention >= RETENTION_INTERVAL_SECONDS:
                    await container.reconciliation.apply_retention()
                    last_retention = time.monotonic()
                if any(stats.values()):
                    logger.info("worker_cycle", extra=log_fields(**stats))
            except Exception:
                logger.exception("worker_cycle_failed")
            if once:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_interval_seconds)
    finally:
        await container.aclose()
        logger.info("worker_stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="executa um único ciclo")
    args = parser.parse_args()
    try:
        asyncio.run(main(once=args.once))
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
