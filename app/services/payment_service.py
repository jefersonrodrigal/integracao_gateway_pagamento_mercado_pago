"""Aplicação do estado do provedor, reconciliação e consulta de pagamentos.

Todo estado financeiro vem de uma resposta da API do Mercado Pago (criação, consulta,
cancelamento, reembolso) — nunca de dados enviados pelo cliente ou do corpo do webhook.

Ordem de locks (para evitar deadlocks): pedido → pagamento → reembolso.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import ConflictError, require
from app.core.logging import log_fields
from app.core.metrics import FINANCIAL_DIVERGENCES, PAYMENT_STATUS_CHANGES, STALE_UPDATES_IGNORED
from app.core.security import Principal
from app.domain.states import (
    SETTLED_PAYMENT_STATUSES,
    OrderStatus,
    PaymentStatus,
    can_transition_payment,
    can_transition_refund,
    order_status_for_payment,
)
from app.integrations.mercadopago.client import MercadoPagoClient
from app.integrations.mercadopago.exceptions import MercadoPagoError
from app.integrations.mercadopago.payments import (
    extract_payment_data,
    map_order_payment_status,
    map_refund_status,
)
from app.integrations.mercadopago.schemas import MPOrder
from app.models.order import Order
from app.models.payment import Payment
from app.repositories.audit import AuditRepository
from app.repositories.orders import OrderRepository
from app.repositories.payments import PaymentRepository
from app.repositories.refunds import RefundRepository
from app.services.order_service import ensure_can_access, set_order_status

logger = logging.getLogger(__name__)


def set_payment_status(
    session: AsyncSession,
    payment: Payment,
    target: PaymentStatus,
    *,
    source: str,
    reason: str,
    details: dict[str, str | None] | None = None,
) -> bool:
    if payment.status == target:
        return False
    if not can_transition_payment(payment.status, target):
        raise ConflictError(
            "Transição de estado do pagamento não permitida.",
            code="invalid_payment_transition",
            details={"from": payment.status.value, "to": target.value},
        )
    previous = payment.status
    payment.status = target
    AuditRepository(session).record(
        entity_type="payment",
        entity_id=payment.id,
        from_status=previous.value,
        to_status=target.value,
        source=source,
        reason=reason,
        details=details,
    )
    PAYMENT_STATUS_CHANGES.labels(target.value, source).inc()
    logger.info(
        "payment_status_changed",
        extra=log_fields(
            payment_id=str(payment.id),
            order_id=str(payment.order_id),
            frm=previous.value,
            to=target.value,
            source=source,
        ),
    )
    return True


def record_divergence(kind: str, **fields: object) -> None:
    FINANCIAL_DIVERGENCES.labels(kind).inc()
    logger.error("financial_divergence", extra=log_fields(kind=kind, alert=True, **fields))


def sync_order_with_payment(
    session: AsyncSession, order: Order, payment: Payment, *, source: str
) -> None:
    desired = order_status_for_payment(payment.status)
    if order.status == desired:
        return
    if order.status == OrderStatus.CANCELLED:
        if payment.status in SETTLED_PAYMENT_STATUSES:
            # Dinheiro recebido para pedido cancelado: não reabre o pedido; exige estorno manual.
            record_divergence(
                "payment_settled_for_cancelled_order",
                order_id=str(order.id),
                payment_id=str(payment.id),
            )
        return
    try:
        set_order_status(
            session, order, desired, source=source, reason=f"payment_{payment.status.value}"
        )
    except ConflictError:
        STALE_UPDATES_IGNORED.labels("order").inc()
        logger.warning(
            "order_transition_ignored",
            extra=log_fields(order_id=str(order.id), frm=order.status.value, to=desired.value),
        )
        return
    if desired == OrderStatus.PAID:
        # Ponto único de liberação: ocorre uma vez, sob lock do pedido. Consumidores de
        # fulfillment leem `state_transitions` (to_status = 'paid') como outbox.
        logger.info(
            "order_paid", extra=log_fields(order_id=str(order.id), payment_id=str(payment.id))
        )


class PaymentService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        client: MercadoPagoClient,
        settings: Settings,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._client = client
        self._settings = settings

    # ------------------------------------------------------------- consulta
    async def get_payment_status(
        self, principal: Principal, order_id: uuid.UUID, *, refresh: bool = False
    ) -> tuple[Order, list[Payment]]:
        async with self._sessionmaker() as session:
            order = ensure_can_access(principal, await OrderRepository(session).get(order_id))
            active = await PaymentRepository(session).get_active_for_order(order.id)
        if refresh and active is not None:
            try:
                await self.reconcile_payment(active.id, source="api_refresh")
            except MercadoPagoError:
                logger.warning(
                    "payment_refresh_failed", extra=log_fields(payment_id=str(active.id))
                )
        async with self._sessionmaker() as session:
            order = ensure_can_access(principal, await OrderRepository(session).get(order_id))
            payments = await PaymentRepository(session).list_for_order(order.id)
        return order, payments

    # ----------------------------------------------------- aplicação de estado
    async def apply_provider_order(
        self, mp_order: MPOrder, *, source: str, payment_id: uuid.UUID | None = None
    ) -> Payment | None:
        """Aplica o estado verificado de uma order do Mercado Pago ao pagamento local."""
        async with self._sessionmaker() as session, session.begin():
            payments = PaymentRepository(session)
            if payment_id is None:
                found = await payments.find_by_provider_reference(
                    provider_order_id=mp_order.id, external_reference=mp_order.external_reference
                )
                if found is None:
                    return None
                payment_id = found.id
            located = await payments.get(payment_id)
            if located is None:
                return None
            order = await OrderRepository(session).get(located.order_id, for_update=True)
            payment = await payments.get(payment_id, for_update=True)
            order = require(order, "order")
            payment = require(payment, "payment")
            await self._apply(session, order, payment, mp_order, source=source)
            return payment

    async def _apply(
        self, session: AsyncSession, order: Order, payment: Payment, mp: MPOrder, *, source: str
    ) -> None:
        if payment.provider_order_id and payment.provider_order_id != mp.id:
            record_divergence("provider_order_mismatch", payment_id=str(payment.id))
            return
        if mp.external_reference and mp.external_reference != payment.external_reference:
            record_divergence("external_reference_mismatch", payment_id=str(payment.id))
            return
        if (
            mp.last_updated_date is not None
            and payment.provider_updated_at is not None
            and mp.last_updated_date < payment.provider_updated_at
        ):
            STALE_UPDATES_IGNORED.labels("payment").inc()
            logger.info(
                "stale_provider_state_ignored", extra=log_fields(payment_id=str(payment.id))
            )
            return

        now = datetime.now(UTC)
        target, raw_status, raw_detail = map_order_payment_status(mp)
        mp_payment = mp.payment
        payment.provider_order_id = mp.id
        payment.provider_status = raw_status
        payment.provider_status_detail = raw_detail
        payment.last_reconciled_at = now
        if mp.last_updated_date is not None:
            payment.provider_updated_at = mp.last_updated_date
        if mp_payment is not None:
            payment.provider_payment_id = mp_payment.id
            if mp_payment.paid_amount is not None:
                payment.paid_amount = mp_payment.paid_amount
            if mp_payment.date_of_expiration is not None:
                payment.expires_at = mp_payment.date_of_expiration
            public_data = extract_payment_data(mp_payment)
            if public_data:
                payment.payment_data = {**(payment.payment_data or {}), **public_data}
            if mp_payment.amount is not None and mp_payment.amount != payment.amount:
                record_divergence(
                    "amount_mismatch",
                    payment_id=str(payment.id),
                    expected=str(payment.amount),
                    provider=str(mp_payment.amount),
                )

        await self._sync_refunds(session, payment, mp, now)

        if target is None:
            record_divergence(
                "unmapped_provider_status",
                payment_id=str(payment.id),
                status=raw_status,
                detail=raw_detail,
            )
            return
        if target != payment.status:
            if not can_transition_payment(payment.status, target):
                STALE_UPDATES_IGNORED.labels("payment").inc()
                logger.warning(
                    "payment_transition_ignored",
                    extra=log_fields(
                        payment_id=str(payment.id), frm=payment.status.value, to=target.value
                    ),
                )
                return
            set_payment_status(
                session,
                payment,
                target,
                source=source,
                reason=f"provider:{raw_status}/{raw_detail}",
                details={"provider_status": raw_status, "provider_status_detail": raw_detail},
            )
        if payment.status == PaymentStatus.REFUNDED:
            payment.refunded_amount = payment.amount
        sync_order_with_payment(session, order, payment, source=source)

    async def _sync_refunds(
        self, session: AsyncSession, payment: Payment, mp: MPOrder, now: datetime
    ) -> None:
        """Atualiza reembolsos locais a partir de `transactions.refunds`, quando presentes."""
        if not mp.transactions.refunds:
            return
        provider_refunds = {r.id: r for r in mp.transactions.refunds}
        repo = RefundRepository(session)
        for refund in await repo.list_for_payment(payment.id):
            provider = provider_refunds.get(refund.provider_refund_id or "")
            if provider is None:
                continue
            refund.provider_status = provider.status
            target = map_refund_status(provider.status)
            if target != refund.status and can_transition_refund(refund.status, target):
                refund.status = target
                refund.processed_at = now
        await session.flush()
        payment.refunded_amount = min(await repo.approved_amount(payment.id), payment.amount)

    # -------------------------------------------------------------- reconciliação
    async def reconcile_payment(
        self, payment_id: uuid.UUID, *, source: str = "reconciliation"
    ) -> Payment | None:
        """Consulta o provedor para descobrir o estado real de uma tentativa."""
        async with self._sessionmaker() as session:
            payment = await PaymentRepository(session).get(payment_id)
        if payment is None:
            return None

        mp_order: MPOrder | None = None
        if payment.provider_order_id:
            mp_order = await self._client.get_order(payment.provider_order_id)
        else:
            begin = payment.created_at - timedelta(hours=1)
            end = datetime.now(UTC) + timedelta(minutes=5)
            matches = await self._client.search_orders_by_reference(
                payment.external_reference, begin, end
            )
            matches = [m for m in matches if m.external_reference == payment.external_reference]
            if len(matches) > 1:
                record_divergence("duplicate_provider_orders", payment_id=str(payment.id))
            if matches:
                # Busca pode retornar dados resumidos: consulta a order completa.
                mp_order = await self._client.get_order(matches[0].id)

        if mp_order is not None:
            return await self.apply_provider_order(mp_order, source=source, payment_id=payment.id)
        return await self._handle_not_found(payment.id)

    async def _handle_not_found(self, payment_id: uuid.UUID) -> Payment | None:
        give_up = timedelta(seconds=self._settings.unknown_payment_give_up_seconds)
        async with self._sessionmaker() as session, session.begin():
            located = await PaymentRepository(session).get(payment_id)
            if located is None:
                return None
            order = await OrderRepository(session).get(located.order_id, for_update=True)
            payment = await PaymentRepository(session).get(payment_id, for_update=True)
            order = require(order, "order")
            payment = require(payment, "payment")
            payment.last_reconciled_at = datetime.now(UTC)
            not_created = payment.status in (PaymentStatus.UNKNOWN, PaymentStatus.CREATED)
            if not_created and datetime.now(UTC) - payment.created_at >= give_up:
                # Sem registro no provedor após a janela de busca: a cobrança não existe.
                set_payment_status(
                    session,
                    payment,
                    PaymentStatus.FAILED,
                    source="reconciliation",
                    reason="not_found_at_provider",
                )
                payment.last_error_code = "not_found_at_provider"
                sync_order_with_payment(session, order, payment, source="reconciliation")
                logger.warning(
                    "payment_given_up", extra=log_fields(payment_id=str(payment.id), alert=True)
                )
            return payment

    async def refresh_refunds_from_provider(self, payment_id: uuid.UUID) -> None:
        """Usado após reembolso: sincroniza o estado do pagamento (refunded/partially_refunded)."""
        async with self._sessionmaker() as session:
            payment = await PaymentRepository(session).get(payment_id)
        if payment is None or not payment.provider_order_id:
            return
        try:
            mp_order = await self._client.get_order(payment.provider_order_id)
        except MercadoPagoError:
            logger.warning("refund_sync_deferred", extra=log_fields(payment_id=str(payment_id)))
            return
        await self.apply_provider_order(mp_order, source="refund", payment_id=payment_id)
