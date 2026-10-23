"""Início de pagamento, captura e cancelamento junto ao Mercado Pago.

Fluxo de criação (sem transação aberta durante a chamada de rede):
1. TX curta: lock do pedido, validação de elegibilidade, registro da tentativa (`created`)
   com `external_reference` e `X-Idempotency-Key` próprios. O índice único parcial
   `uq_payments_one_active_per_order` impede duas tentativas ativas para o mesmo pedido.
2. Chamada ao provedor (retries só com a mesma chave idempotente).
3. TX curta: aplica o estado retornado. Se o resultado for desconhecido, marca `unknown`
   e reconcilia por consulta (busca por `external_reference`) — nunca cria outra cobrança.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    PaymentProviderError,
    ServiceUnavailableError,
    require,
)
from app.core.logging import log_fields
from app.core.metrics import PAYMENT_ATTEMPTS, PAYMENT_OUTCOME_UNKNOWN
from app.core.security import Principal
from app.domain.states import OrderStatus, PaymentMethod, PaymentStatus
from app.integrations.mercadopago.client import MercadoPagoClient
from app.integrations.mercadopago.exceptions import (
    MercadoPagoAPIError,
    MercadoPagoError,
    MercadoPagoOutcomeUnknownError,
    MercadoPagoUnavailableError,
)
from app.integrations.mercadopago.orders import OrderPaymentInput, build_create_order_payload
from app.integrations.mercadopago.schemas import MPOrder
from app.models.order import Order
from app.models.payment import Payment
from app.repositories.orders import OrderRepository
from app.repositories.payments import PaymentRepository
from app.schemas.payments import (
    BoletoPaymentCreate,
    CardPaymentCreate,
    PaymentCreate,
    PixPaymentCreate,
)
from app.services.order_service import cancel_order_locally, ensure_can_access, set_order_status
from app.services.payment_service import PaymentService, set_payment_status, sync_order_with_payment

logger = logging.getLogger(__name__)

# Respostas definitivas em que a cobrança não foi criada por erro de dados/regra.
_CLIENT_SIDE_FAILURES = frozenset({400, 402, 409, 422})


class CheckoutService:
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

    # ------------------------------------------------------------- pagamento
    async def start_payment(
        self,
        principal: Principal,
        order_id: uuid.UUID,
        data: PaymentCreate,
        client_idempotency_key: str,
    ) -> Payment:
        payment, resume = await self._register_attempt(
            principal, order_id, data, client_idempotency_key
        )
        if not resume:
            return payment

        PAYMENT_ATTEMPTS.labels(payment.method.value).inc()
        payload = build_create_order_payload(self._payment_input(payment, data))
        try:
            mp_order = await self._client.create_order(payload, payment.provider_idempotency_key)
        except MercadoPagoOutcomeUnknownError as exc:
            return await self._handle_unknown(payment, exc)
        except MercadoPagoUnavailableError as exc:
            await self._mark_failed(payment.id, "provider_unavailable", str(exc))
            raise ServiceUnavailableError(
                "Provedor de pagamento indisponível. Tente novamente.",
                code="payment_provider_unavailable",
            ) from None
        except MercadoPagoAPIError as exc:
            return await self._handle_api_error(payment, exc)

        applied = await self._payments.apply_provider_order(
            mp_order, source="api", payment_id=payment.id
        )
        return applied or payment

    async def _register_attempt(
        self,
        principal: Principal,
        order_id: uuid.UUID,
        data: PaymentCreate,
        client_key: str,
    ) -> tuple[Payment, bool]:
        """Retorna (pagamento, deve_chamar_provedor)."""
        if (
            isinstance(data, CardPaymentCreate)
            and data.installments > self._settings.max_installments
        ):
            raise BusinessRuleError(
                "Número de parcelas acima do permitido.", code="invalid_installments"
            )
        try:
            async with self._sessionmaker() as session, session.begin():
                orders = OrderRepository(session)
                payments = PaymentRepository(session)
                order = ensure_can_access(principal, await orders.get(order_id, for_update=True))

                active = await payments.get_active_for_order(order.id, for_update=True)
                if active is not None:
                    if active.client_idempotency_key == client_key:
                        # Repetição da mesma requisição: retoma se nunca chegou ao provedor.
                        return active, active.status == PaymentStatus.CREATED
                    raise ConflictError(
                        "Já existe um pagamento em andamento para este pedido.",
                        code="payment_in_progress",
                        details={"payment_id": str(active.id), "status": active.status.value},
                    )
                if order.status != OrderStatus.PENDING_PAYMENT:
                    raise ConflictError(
                        "Pedido não está elegível para pagamento.",
                        code="order_not_payable",
                        details={"status": order.status.value},
                    )

                payment = self._new_payment(order, data, client_key)
                payments.add(payment)
                await session.flush()
                set_order_status(
                    session, order, OrderStatus.PROCESSING, source="api", reason="payment_started"
                )
        except IntegrityError:
            raise ConflictError(
                "Já existe um pagamento em andamento para este pedido.", code="payment_in_progress"
            ) from None
        logger.info(
            "payment_attempt_registered",
            extra=log_fields(
                payment_id=str(payment.id), order_id=str(order_id), method=payment.method.value
            ),
        )
        return payment, True

    def _new_payment(self, order: Order, data: PaymentCreate, client_key: str) -> Payment:
        payment_id = uuid.uuid4()
        if isinstance(data, CardPaymentCreate):
            method = PaymentMethod(data.method)
            method_id = data.payment_method_id
            installments = data.installments
            capture_mode = "automatic" if data.capture else "manual"
        else:
            method = (
                PaymentMethod.PIX if isinstance(data, PixPaymentCreate) else PaymentMethod.BOLETO
            )
            method_id = method.value
            installments = 1
            capture_mode = "automatic"
        return Payment(
            id=payment_id,
            order_id=order.id,
            status=PaymentStatus.CREATED,
            method=method,
            payment_method_id=method_id,
            amount=order.total_amount,  # valor sempre do pedido calculado no servidor
            currency=order.currency,
            installments=installments,
            capture_mode=capture_mode,
            external_reference=f"pay_{payment_id.hex}",
            provider_idempotency_key=f"create-{payment_id}",
            client_idempotency_key=client_key,
            create_attempts=1,
        )

    def _payment_input(self, payment: Payment, data: PaymentCreate) -> OrderPaymentInput:
        payer: dict[str, Any] = data.payer.model_dump(mode="json", exclude_none=True)
        base = OrderPaymentInput(
            external_reference=payment.external_reference,
            amount=payment.amount,
            method=payment.method,
            payment_method_id=payment.payment_method_id,
            payer=payer,
            description=f"Pedido {payment.order_id}",
        )
        if isinstance(data, CardPaymentCreate):
            return replace(
                base,
                card_token=data.token.get_secret_value(),
                installments=data.installments,
                capture_manual=not data.capture,
                statement_descriptor=self._settings.mercado_pago_statement_descriptor,
            )
        expiration = (
            self._settings.boleto_expiration
            if isinstance(data, BoletoPaymentCreate)
            else self._settings.pix_expiration
        )
        return replace(base, expiration_time=expiration)

    async def _handle_unknown(
        self, payment: Payment, exc: MercadoPagoOutcomeUnknownError
    ) -> Payment:
        PAYMENT_OUTCOME_UNKNOWN.inc()
        logger.warning(
            "payment_outcome_unknown",
            extra=log_fields(payment_id=str(payment.id), reason=exc.reason),
        )
        async with self._sessionmaker() as session, session.begin():
            order = await OrderRepository(session).get(payment.order_id, for_update=True)
            locked = await PaymentRepository(session).get(payment.id, for_update=True)
            order = require(order, "order")
            locked = require(locked, "locked")
            if locked.status == PaymentStatus.CREATED:
                set_payment_status(
                    session, locked, PaymentStatus.UNKNOWN, source="api", reason=exc.reason
                )
            locked.last_error_code = (exc.code or "outcome_unknown")[:64]
            payment = locked
        try:
            reconciled = await self._payments.reconcile_payment(payment.id, source="api")
        except MercadoPagoError:
            logger.info(
                "immediate_reconciliation_deferred", extra=log_fields(payment_id=str(payment.id))
            )
            reconciled = None
        return reconciled or payment

    async def _handle_api_error(self, payment: Payment, exc: MercadoPagoAPIError) -> Payment:
        # 402: a order foi criada, mas a transação falhou; o corpo traz o estado real.
        if exc.payload and exc.payload.get("id") and exc.payload.get("transactions"):
            try:
                mp_order = MPOrder.model_validate(exc.payload)
            except ValueError:
                mp_order = None
            if mp_order is not None:
                applied = await self._payments.apply_provider_order(
                    mp_order, source="api", payment_id=payment.id
                )
                if applied is not None:
                    return applied
        code = exc.code or f"http_{exc.status_code}"
        if exc.status_code in _CLIENT_SIDE_FAILURES:
            target = PaymentStatus.REJECTED if exc.status_code == 402 else PaymentStatus.FAILED
            return await self._mark_failed(payment.id, code, exc.provider_message, target=target)
        await self._mark_failed(payment.id, code, exc.provider_message)
        logger.error(
            "payment_provider_rejected_request",
            extra=log_fields(
                payment_id=str(payment.id), status=exc.status_code, code=code, alert=True
            ),
        )
        raise PaymentProviderError(code="payment_provider_error")

    async def _mark_failed(
        self,
        payment_id: uuid.UUID,
        code: str,
        message: str,
        *,
        target: PaymentStatus = PaymentStatus.FAILED,
    ) -> Payment:
        async with self._sessionmaker() as session, session.begin():
            located = await PaymentRepository(session).get(payment_id)
            located = require(located, "located")
            order = await OrderRepository(session).get(located.order_id, for_update=True)
            payment = await PaymentRepository(session).get(payment_id, for_update=True)
            order = require(order, "order")
            payment = require(payment, "payment")
            if payment.status in (PaymentStatus.CREATED, PaymentStatus.UNKNOWN):
                set_payment_status(session, payment, target, source="api", reason=code)
                sync_order_with_payment(session, order, payment, source="api")
            payment.last_error_code = code[:64]
            payment.last_error_message = message[:255] or None
            return payment

    # ---------------------------------------------------------------- captura
    async def capture_payment(self, principal: Principal, order_id: uuid.UUID) -> Payment:
        principal.require_admin()
        async with self._sessionmaker() as session:
            order = ensure_can_access(principal, await OrderRepository(session).get(order_id))
            payment = await PaymentRepository(session).get_active_for_order(order.id)
        if (
            payment is None
            or payment.status != PaymentStatus.AUTHORIZED
            or not payment.provider_order_id
        ):
            raise ConflictError(
                "Não há pagamento autorizado aguardando captura.", code="nothing_to_capture"
            )
        try:
            mp_order = await self._client.capture_order(
                payment.provider_order_id, f"capture-{payment.id}"
            )
        except (MercadoPagoOutcomeUnknownError, MercadoPagoUnavailableError):
            return await self._reconcile_or_unavailable(payment.id)
        except MercadoPagoAPIError as exc:
            await self._reconcile_quietly(payment.id)
            raise ConflictError(
                "O provedor recusou a captura.", code=exc.code or "capture_rejected"
            ) from None
        applied = await self._payments.apply_provider_order(
            mp_order, source="api", payment_id=payment.id
        )
        return applied or payment

    # ----------------------------------------------------------- cancelamento
    async def cancel_order(self, principal: Principal, order_id: uuid.UUID) -> Order:
        provider_order_id, payment_id = await self._prepare_cancel(principal, order_id)
        if provider_order_id is not None and payment_id is not None:
            mp_order = await self._cancel_at_provider(provider_order_id, payment_id)
            await self._payments.apply_provider_order(mp_order, source="api", payment_id=payment_id)
            async with self._sessionmaker() as session, session.begin():
                order = ensure_can_access(
                    principal, await OrderRepository(session).get(order_id, for_update=True)
                )
                active = await PaymentRepository(session).get_active_for_order(order.id)
                if active is None and order.status == OrderStatus.PENDING_PAYMENT:
                    await cancel_order_locally(
                        session, order, source="api", reason="cancelled_by_request"
                    )
                elif order.status != OrderStatus.CANCELLED:
                    raise ConflictError(
                        "O pagamento não pôde ser cancelado; verifique o estado atual.",
                        code="order_not_cancellable",
                        details={"status": order.status.value},
                    )
        async with self._sessionmaker() as session:
            return ensure_can_access(principal, await OrderRepository(session).get(order_id))

    async def _prepare_cancel(
        self, principal: Principal, order_id: uuid.UUID
    ) -> tuple[str | None, uuid.UUID | None]:
        async with self._sessionmaker() as session, session.begin():
            order = ensure_can_access(
                principal, await OrderRepository(session).get(order_id, for_update=True)
            )
            if order.status == OrderStatus.CANCELLED:
                return None, None
            if order.status not in (OrderStatus.PENDING_PAYMENT, OrderStatus.PROCESSING):
                raise ConflictError(
                    "Pedido pago não pode ser cancelado; solicite reembolso.",
                    code="order_not_cancellable",
                    details={"status": order.status.value},
                )
            active = await PaymentRepository(session).get_active_for_order(
                order.id, for_update=True
            )
            if active is None:
                await cancel_order_locally(
                    session, order, source="api", reason="cancelled_by_request"
                )
                return None, None
            if (
                active.status in (PaymentStatus.CREATED, PaymentStatus.UNKNOWN)
                or not active.provider_order_id
            ):
                raise ConflictError(
                    "Resultado do pagamento ainda em verificação. Tente novamente em instantes.",
                    code="payment_outcome_pending",
                )
            return active.provider_order_id, active.id

    async def _cancel_at_provider(self, provider_order_id: str, payment_id: uuid.UUID) -> MPOrder:
        try:
            return await self._client.cancel_order(provider_order_id, f"cancel-{payment_id}")
        except MercadoPagoAPIError as exc:
            if exc.status_code != 409:
                raise PaymentProviderError(code=exc.code or "cancel_failed") from None
        except MercadoPagoOutcomeUnknownError:
            pass
        except MercadoPagoUnavailableError:
            raise ServiceUnavailableError(code="payment_provider_unavailable") from None
        # Já cancelada, não cancelável ou resultado incerto: consulta o estado real.
        try:
            return await self._client.get_order(provider_order_id)
        except MercadoPagoError:
            raise ServiceUnavailableError(code="payment_provider_unavailable") from None

    # ---------------------------------------------------------------- helpers
    async def _reconcile_quietly(self, payment_id: uuid.UUID) -> Payment | None:
        try:
            return await self._payments.reconcile_payment(payment_id, source="api")
        except MercadoPagoError:
            return None

    async def _reconcile_or_unavailable(self, payment_id: uuid.UUID) -> Payment:
        payment = await self._reconcile_quietly(payment_id)
        if payment is None:
            raise ServiceUnavailableError(code="payment_provider_unavailable")
        return payment
