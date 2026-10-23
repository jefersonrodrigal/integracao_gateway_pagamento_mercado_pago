"""Reembolsos totais e parciais via `POST /v1/orders/{id}/refund`.

Proteções:
- Lock pedido → pagamento ao registrar o reembolso; o saldo reembolsável considera
  reembolsos aprovados e pendentes, e o índice `uq_refunds_one_pending_per_payment`
  permite apenas um reembolso em andamento por pagamento.
- `X-Idempotency-Key` próprio por reembolso, reutilizado em retries.
- Resultado desconhecido: o reembolso permanece `pending` e é reconciliado pelo worker.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import BusinessRuleError, ConflictError, require
from app.core.logging import log_fields
from app.core.money import ZERO, format_amount, to_money
from app.core.security import Principal
from app.domain.states import RefundStatus, can_transition_refund
from app.integrations.mercadopago.client import MercadoPagoClient
from app.integrations.mercadopago.exceptions import (
    MercadoPagoAPIError,
    MercadoPagoError,
    MercadoPagoOutcomeUnknownError,
    MercadoPagoUnavailableError,
)
from app.integrations.mercadopago.payments import map_refund_status
from app.integrations.mercadopago.schemas import MPOrder, MPRefund
from app.models.refund import Refund
from app.repositories.audit import AuditRepository
from app.repositories.orders import OrderRepository
from app.repositories.payments import PaymentRepository
from app.repositories.refunds import RefundRepository
from app.schemas.refunds import RefundCreate
from app.services.order_service import ensure_can_access
from app.services.payment_service import PaymentService, record_divergence

logger = logging.getLogger(__name__)


def set_refund_status(
    session: AsyncSession, refund: Refund, target: RefundStatus, *, source: str, reason: str
) -> None:
    if refund.status == target or not can_transition_refund(refund.status, target):
        return
    previous = refund.status
    refund.status = target
    if target != RefundStatus.PENDING:
        refund.processed_at = datetime.now(UTC)
    AuditRepository(session).record(
        entity_type="refund",
        entity_id=refund.id,
        from_status=previous.value,
        to_status=target.value,
        source=source,
        reason=reason,
    )


class RefundService:
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

    async def request_refund(
        self,
        principal: Principal,
        order_id: uuid.UUID,
        data: RefundCreate,
        client_idempotency_key: str,
    ) -> Refund:
        principal.require_admin()
        refund, provider_order_id, provider_payment_id = await self._register(
            principal, order_id, data, client_idempotency_key
        )
        transactions = None
        if not refund.is_full:
            transactions = [{"id": provider_payment_id, "amount": format_amount(refund.amount)}]
        try:
            mp_order = await self._client.refund_order(
                provider_order_id, refund.provider_idempotency_key, transactions
            )
        except (MercadoPagoOutcomeUnknownError, MercadoPagoUnavailableError) as exc:
            logger.warning(
                "refund_outcome_unknown", extra=log_fields(refund_id=str(refund.id), error=str(exc))
            )
            return await self._update_error(refund.id, "outcome_unknown", str(exc), final=False)
        except MercadoPagoAPIError as exc:
            final = exc.status_code in {400, 404, 409, 422}
            updated = await self._update_error(
                refund.id, exc.code or f"http_{exc.status_code}", exc.provider_message, final=final
            )
            if final:
                raise ConflictError(
                    "O provedor recusou o reembolso.",
                    code=exc.code or "refund_rejected",
                    details={"refund_id": str(refund.id)},
                ) from None
            return updated

        updated = await self._apply_refund_response(refund.id, mp_order, source="api")
        await self._payments.refresh_refunds_from_provider(refund.payment_id)
        return updated

    async def _register(
        self, principal: Principal, order_id: uuid.UUID, data: RefundCreate, client_key: str
    ) -> tuple[Refund, str, str]:
        try:
            async with self._sessionmaker() as session, session.begin():
                order = ensure_can_access(
                    principal, await OrderRepository(session).get(order_id, for_update=True)
                )
                payment = await PaymentRepository(session).get_settled_for_order(
                    order.id, for_update=True
                )
                if (
                    payment is None
                    or not payment.provider_order_id
                    or not payment.provider_payment_id
                ):
                    raise ConflictError(
                        "Pedido sem pagamento aprovado para reembolso.", code="not_refundable"
                    )
                repo = RefundRepository(session)
                committed = await repo.committed_amount(payment.id)
                refundable = to_money(payment.amount - committed)
                if refundable <= ZERO:
                    raise ConflictError(
                        "Pagamento já totalmente reembolsado.", code="not_refundable"
                    )
                amount = to_money(data.amount) if data.amount is not None else refundable
                if amount <= ZERO or amount > refundable:
                    raise BusinessRuleError(
                        "Valor de reembolso inválido.",
                        code="invalid_refund_amount",
                        details={"refundable": format_amount(refundable)},
                    )
                # Reembolso total no provedor só quando nada foi reembolsado antes.
                is_full = committed == ZERO and amount == payment.amount
                refund_id = uuid.uuid4()
                refund = Refund(
                    id=refund_id,
                    payment_id=payment.id,
                    order_id=order.id,
                    status=RefundStatus.PENDING,
                    amount=amount,
                    is_full=is_full,
                    reason=data.reason,
                    requested_by=principal.subject,
                    provider_idempotency_key=f"refund-{refund_id}",
                    client_idempotency_key=client_key,
                )
                repo.add(refund)
                await session.flush()
                AuditRepository(session).record(
                    entity_type="refund",
                    entity_id=refund.id,
                    from_status=None,
                    to_status=RefundStatus.PENDING.value,
                    source="api",
                    reason="refund_requested",
                    details={"amount": format_amount(amount)},
                )
                return refund, payment.provider_order_id, payment.provider_payment_id
        except IntegrityError:
            raise ConflictError(
                "Já existe um reembolso em andamento para este pagamento.",
                code="refund_in_progress",
            ) from None

    async def _apply_refund_response(
        self, refund_id: uuid.UUID, mp_order: MPOrder, *, source: str
    ) -> Refund:
        async with self._sessionmaker() as session, session.begin():
            repo = RefundRepository(session)
            refund = await repo.get(refund_id, for_update=True)
            refund = require(refund, "refund")
            refund.last_reconciled_at = datetime.now(UTC)
            match = self._match_provider_refund(
                refund,
                mp_order.transactions.refunds,
                await repo.known_provider_ids(refund.payment_id),
            )
            if match is None:
                if mp_order.status_detail in {"refunded", "partially_refunded"} and refund.is_full:
                    set_refund_status(
                        session,
                        refund,
                        RefundStatus.APPROVED,
                        source=source,
                        reason="order_refunded",
                    )
                return refund
            refund.provider_refund_id = match.id
            refund.provider_status = match.status
            if match.amount is not None and to_money(match.amount) != refund.amount:
                record_divergence(
                    "refund_amount_mismatch",
                    refund_id=str(refund.id),
                    expected=str(refund.amount),
                    provider=str(match.amount),
                )
            set_refund_status(
                session,
                refund,
                map_refund_status(match.status),
                source=source,
                reason=f"provider:{match.status}",
            )
            return refund

    @staticmethod
    def _match_provider_refund(
        refund: Refund, provider_refunds: list[MPRefund], known_ids: set[str]
    ) -> MPRefund | None:
        if refund.provider_refund_id:
            return next((r for r in provider_refunds if r.id == refund.provider_refund_id), None)
        candidates = [r for r in provider_refunds if r.id not in known_ids]
        exact = [
            r for r in candidates if r.amount is not None and to_money(r.amount) == refund.amount
        ]
        if exact:
            return exact[-1]
        return candidates[-1] if len(candidates) == 1 else None

    async def _update_error(
        self, refund_id: uuid.UUID, code: str, message: str, *, final: bool
    ) -> Refund:
        async with self._sessionmaker() as session, session.begin():
            refund = await RefundRepository(session).get(refund_id, for_update=True)
            refund = require(refund, "refund")
            refund.last_error_code = code[:64]
            refund.last_error_message = message[:255] or None
            if final:
                set_refund_status(session, refund, RefundStatus.REJECTED, source="api", reason=code)
            return refund

    # --------------------------------------------------------- reconciliação
    async def reconcile_refund(self, refund_id: uuid.UUID) -> Refund | None:
        async with self._sessionmaker() as session:
            refund = await RefundRepository(session).get(refund_id)
            payment = await PaymentRepository(session).get(refund.payment_id) if refund else None
        if refund is None or payment is None or not payment.provider_order_id:
            return refund
        try:
            mp_order = await self._client.get_order(payment.provider_order_id)
        except MercadoPagoError:
            return refund
        updated = await self._apply_refund_response(refund.id, mp_order, source="reconciliation")
        if updated.status == RefundStatus.PENDING:
            age = datetime.now(UTC) - updated.created_at
            if age >= timedelta(seconds=self._settings.pending_refund_give_up_seconds):
                async with self._sessionmaker() as session, session.begin():
                    locked = await RefundRepository(session).get(refund.id, for_update=True)
                    locked = require(locked, "locked")
                    set_refund_status(
                        session,
                        locked,
                        RefundStatus.FAILED,
                        source="reconciliation",
                        reason="not_confirmed_by_provider",
                    )
                    locked.last_reconciled_at = datetime.now(UTC)
                    updated = locked
                logger.error(
                    "refund_unconfirmed_requires_review",
                    extra=log_fields(refund_id=str(refund.id), alert=True),
                )
        await self._payments.apply_provider_order(
            mp_order, source="reconciliation", payment_id=payment.id
        )
        return updated
