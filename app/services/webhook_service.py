"""Recebimento e processamento de notificações do Mercado Pago.

Recebimento (rápido, dentro dos 22 s exigidos pelo provedor):
    valida a assinatura → persiste o evento (deduplicado) → responde 200.
Processamento (worker durável ou em seguida, em background):
    reserva o evento (SKIP LOCKED) → consulta `GET /v1/orders/{id}` → aplica o estado
    verificado com regras de transição → marca processado. Falhas são reagendadas com
    backoff exponencial + jitter; ao esgotar as tentativas o evento vai para `dead`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, NotFoundError, require
from app.core.logging import log_fields
from app.core.metrics import WEBHOOK_DEAD_LETTERS, WEBHOOK_EVENTS
from app.core.security import Principal
from app.domain.states import EventStatus
from app.integrations.mercadopago.client import MercadoPagoClient
from app.integrations.mercadopago.exceptions import MercadoPagoAPIError, MercadoPagoError
from app.integrations.mercadopago.webhooks import verify_signature
from app.models.payment_event import PaymentEvent
from app.repositories.payment_events import PaymentEventRepository
from app.services.payment_service import PaymentService

logger = logging.getLogger(__name__)

SUPPORTED_TOPICS = frozenset({"order"})
LOCK_DURATION = timedelta(seconds=120)


def _minimized_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Guarda apenas o necessário para auditoria (minimização de dados — LGPD)."""
    raw_data = body.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    return {
        "action": body.get("action"),
        "type": body.get("type"),
        "api_version": body.get("api_version"),
        "live_mode": body.get("live_mode"),
        "date_created": body.get("date_created"),
        "data": {
            key: data.get(key)
            for key in ("id", "status", "status_detail", "external_reference")
            if key in data
        },
    }


def retry_delay(attempts: int) -> timedelta:
    base = min(30 * (2 ** max(attempts - 1, 0)), 6 * 3600)
    return timedelta(seconds=base * random.uniform(0.8, 1.2))  # noqa: S311


class WebhookService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: MercadoPagoClient,
        payment_service: PaymentService,
        settings: Settings,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._client = client
        self._payments = payment_service
        self._settings = settings

    # ----------------------------------------------------------- recebimento
    async def receive(
        self,
        *,
        raw_body: bytes,
        x_signature: str | None,
        x_request_id: str | None,
        query_data_id: str | None,
        query_type: str | None,
    ) -> tuple[str, uuid.UUID | None]:
        try:
            body = json.loads(raw_body) if raw_body else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        body_data = body.get("data") if isinstance(body.get("data"), dict) else {}
        body_data_id = str(body_data.get("id")) if body_data and body_data.get("id") else None

        signed_id = query_data_id or body_data_id
        result = verify_signature(
            secret=self._settings.mercado_pago_webhook_secret.get_secret_value(),
            x_signature=x_signature,
            x_request_id=x_request_id,
            data_id=signed_id,
            tolerance_seconds=self._settings.webhook_signature_tolerance_seconds,
        )
        if not result.valid:
            WEBHOOK_EVENTS.labels("invalid_signature").inc()
            logger.warning("webhook_rejected", extra=log_fields(reason=result.reason))
            raise AuthenticationError("Assinatura do webhook inválida.", code="invalid_signature")

        if query_data_id and body_data_id and query_data_id != body_data_id:
            logger.warning("webhook_id_mismatch", extra=log_fields(query=query_data_id))
        event_type = (query_type or str(body.get("type") or "")).lower()[:64] or "unknown"
        action = str(body.get("action"))[:64] if body.get("action") else None
        resource_id = signed_id[:64] if signed_id else None

        body_hash = hashlib.sha256(raw_body).hexdigest()
        dedup_key = hashlib.sha256(
            f"{event_type}|{resource_id}|{action}|{body_hash}".encode()
        ).hexdigest()
        supported = event_type in SUPPORTED_TOPICS and resource_id is not None
        values = {
            "dedup_key": dedup_key,
            "external_event_id": (x_request_id or "")[:128] or None,
            "event_type": event_type,
            "action": action,
            "resource_id": resource_id,
            "payload": _minimized_payload(body),
            "status": EventStatus.PENDING if supported else EventStatus.IGNORED,
            "result": None if supported else "unsupported_topic",
        }
        async with self._sessionmaker() as session, session.begin():
            event_id, inserted = await PaymentEventRepository(session).insert_or_count_duplicate(
                values
            )

        outcome = "received" if supported else "ignored"
        if not inserted:
            outcome = "duplicate"
        WEBHOOK_EVENTS.labels(outcome).inc()
        logger.info(
            "webhook_received",
            extra=log_fields(
                event_id=str(event_id), type=event_type, action=action, outcome=outcome
            ),
        )
        return outcome, event_id

    # ---------------------------------------------------------- processamento
    async def process_event(self, event_id: uuid.UUID) -> bool:
        events = await self._claim(limit=1, event_id=event_id)
        for event in events:
            await self._process(event)
        return bool(events)

    async def process_due(self, limit: int) -> int:
        events = await self._claim(limit=limit)
        for event in events:
            await self._process(event)
        return len(events)

    async def reprocess(self, principal: Principal, event_id: uuid.UUID) -> PaymentEvent:
        """Reprocessamento manual seguro (o processamento é idempotente)."""
        principal.require_admin()
        async with self._sessionmaker() as session, session.begin():
            event = await PaymentEventRepository(session).get(event_id, for_update=True)
            if event is None:
                raise NotFoundError("Evento não encontrado.", code="event_not_found")
            if event.event_type in SUPPORTED_TOPICS and event.resource_id:
                event.status = EventStatus.PENDING
                event.next_attempt_at = datetime.now(UTC)
                event.locked_until = None
        await self.process_event(event_id)
        async with self._sessionmaker() as session:
            refreshed = await PaymentEventRepository(session).get(event_id)
            refreshed = require(refreshed, "refreshed")
            return refreshed

    async def _claim(self, *, limit: int, event_id: uuid.UUID | None = None) -> list[PaymentEvent]:
        async with self._sessionmaker() as session, session.begin():
            return await PaymentEventRepository(session).claim(
                now=datetime.now(UTC), lock_for=LOCK_DURATION, limit=limit, event_id=event_id
            )

    async def _process(self, event: PaymentEvent) -> None:
        event.resource_id = require(event.resource_id, "event.resource_id")
        try:
            mp_order = await self._client.get_order(event.resource_id)
        except MercadoPagoAPIError as exc:
            if exc.status_code == 404:
                await self._finish(event.id, EventStatus.IGNORED, "order_not_found_at_provider")
            else:
                await self._fail(event.id, f"provider_http_{exc.status_code}:{exc.code}")
            return
        except MercadoPagoError as exc:
            await self._fail(event.id, f"provider_unavailable:{exc}")
            return
        except ValueError:
            await self._finish(event.id, EventStatus.IGNORED, "invalid_resource_id")
            return

        try:
            payment = await self._payments.apply_provider_order(mp_order, source="webhook")
        except Exception as exc:
            logger.exception("webhook_processing_error", extra=log_fields(event_id=str(event.id)))
            await self._fail(event.id, f"processing_error:{type(exc).__name__}")
            return
        if payment is None:
            await self._finish(event.id, EventStatus.IGNORED, "unknown_order")
            return
        await self._finish(
            event.id,
            EventStatus.PROCESSED,
            f"payment_status={payment.status.value}",
            payment_id=payment.id,
            order_id=payment.order_id,
        )

    async def _finish(
        self,
        event_id: uuid.UUID,
        status: EventStatus,
        result: str,
        *,
        payment_id: uuid.UUID | None = None,
        order_id: uuid.UUID | None = None,
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            event = await PaymentEventRepository(session).get(event_id, for_update=True)
            event = require(event, "event")
            event.status = status
            event.result = result[:255]
            event.processed_at = datetime.now(UTC)
            event.locked_until = None
            event.last_error = None
            event.payment_id = payment_id or event.payment_id
            event.order_id = order_id or event.order_id
        WEBHOOK_EVENTS.labels(status.value).inc()

    async def _fail(self, event_id: uuid.UUID, error: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            event = await PaymentEventRepository(session).get(event_id, for_update=True)
            event = require(event, "event")
            event.last_error = error[:500]
            event.locked_until = None
            if event.attempts >= self._settings.webhook_max_attempts:
                event.status = EventStatus.DEAD
                WEBHOOK_DEAD_LETTERS.inc()
                logger.error(
                    "webhook_dead_letter",
                    extra=log_fields(event_id=str(event.id), attempts=event.attempts, alert=True),
                )
            else:
                event.status = EventStatus.FAILED
                event.next_attempt_at = datetime.now(UTC) + retry_delay(event.attempts)
                logger.warning(
                    "webhook_processing_failed",
                    extra=log_fields(event_id=str(event.id), attempts=event.attempts, error=error),
                )
