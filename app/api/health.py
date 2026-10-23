from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select, text

from app.api.deps import ContainerDep
from app.domain.states import EventStatus, PaymentStatus
from app.models.payment import Payment
from app.models.payment_event import PaymentEvent

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health/live", summary="Liveness")
async def live() -> dict[str, str]:
    """O processo está de pé (não verifica dependências)."""
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness")
async def ready(container: ContainerDep) -> JSONResponse:
    """Verifica PostgreSQL e expõe indicadores operacionais (filas e pendências).

    Não chama o Mercado Pago a cada verificação, para não consumir cota do provedor.
    """
    checks: dict[str, object] = {}
    healthy = True
    try:
        async with asyncio.timeout(3):
            async with container.sessionmaker() as session:
                await session.execute(text("SELECT 1"))
                dead = await session.scalar(
                    select(func.count()).where(PaymentEvent.status == EventStatus.DEAD)
                )
                backlog = await session.scalar(
                    select(func.count()).where(
                        PaymentEvent.status.in_([EventStatus.PENDING, EventStatus.FAILED])
                    )
                )
                unknown = await session.scalar(
                    select(func.count()).where(Payment.status == PaymentStatus.UNKNOWN)
                )
        checks["database"] = "ok"
        checks["webhook_backlog"] = backlog
        checks["webhook_dead_letters"] = dead
        checks["payments_outcome_unknown"] = unknown
    except Exception:
        logger.exception("readiness_database_failed")
        checks["database"] = "unavailable"
        healthy = False
    checks["mercadopago_configured"] = bool(
        container.settings.mercado_pago_access_token.get_secret_value()
    )
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )


@router.get("/metrics", include_in_schema=False)
async def metrics(container: ContainerDep) -> Response:
    if not container.settings.metrics_enabled:
        return Response(status_code=404)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
