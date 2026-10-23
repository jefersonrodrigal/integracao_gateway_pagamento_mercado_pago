"""Tarefas periódicas: reconciliação com o provedor e políticas de retenção."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import log_fields
from app.integrations.mercadopago.exceptions import MercadoPagoError
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.payment_events import PaymentEventRepository
from app.repositories.payments import PaymentRepository
from app.repositories.refunds import RefundRepository
from app.services.payment_service import PaymentService
from app.services.refund_service import RefundService

logger = logging.getLogger(__name__)


class ReconciliationService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        payment_service: PaymentService,
        refund_service: RefundService,
        settings: Settings,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._payments = payment_service
        self._refunds = refund_service
        self._settings = settings

    async def reconcile_payments(self, limit: int) -> int:
        now = datetime.now(UTC)
        async with self._sessionmaker() as session:
            candidates = await PaymentRepository(session).list_for_reconciliation(
                stale_before=now - timedelta(seconds=self._settings.reconcile_interval_seconds),
                unknown_before=now - timedelta(seconds=30),
                limit=limit,
            )
        for payment in candidates:
            try:
                await self._payments.reconcile_payment(payment.id)
            except MercadoPagoError as exc:
                logger.warning(
                    "reconciliation_failed",
                    extra=log_fields(payment_id=str(payment.id), error=str(exc)),
                )
                await self._touch(payment.id)
        return len(candidates)

    async def _touch(self, payment_id: object) -> None:
        async with self._sessionmaker() as session, session.begin():
            payment = await PaymentRepository(session).get(payment_id, for_update=True)  # type: ignore[arg-type]
            if payment is not None:
                payment.last_reconciled_at = datetime.now(UTC)

    async def reconcile_refunds(self, limit: int) -> int:
        stale_before = datetime.now(UTC) - timedelta(seconds=120)
        async with self._sessionmaker() as session:
            pending = await RefundRepository(session).list_pending(
                stale_before=stale_before, limit=limit
            )
        for refund in pending:
            await self._refunds.reconcile_refund(refund.id)
        return len(pending)

    async def apply_retention(self) -> dict[str, int]:
        now = datetime.now(UTC)
        async with self._sessionmaker() as session, session.begin():
            expired_keys = await IdempotencyRepository(session).purge_expired(now)
            purged_payloads = await PaymentEventRepository(session).purge_payloads(
                now - timedelta(days=self._settings.payment_event_retention_days)
            )
        if expired_keys or purged_payloads:
            logger.info(
                "retention_applied",
                extra=log_fields(idempotency_records=expired_keys, event_payloads=purged_payloads),
            )
        return {"idempotency_records": expired_keys, "event_payloads": purged_payloads}
