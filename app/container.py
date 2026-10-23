"""Composição das dependências (engine, cliente do provedor e serviços)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine, create_sessionmaker
from app.integrations.mercadopago.client import MercadoPagoClient
from app.services.checkout_service import CheckoutService
from app.services.idempotency_service import IdempotencyService
from app.services.order_service import OrderService
from app.services.payment_methods_service import PaymentMethodsService
from app.services.payment_service import PaymentService
from app.services.reconciliation_service import ReconciliationService
from app.services.refund_service import RefundService
from app.services.webhook_service import WebhookService


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    mp_client: MercadoPagoClient
    idempotency: IdempotencyService
    orders: OrderService
    payments: PaymentService
    checkout: CheckoutService
    refunds: RefundService
    webhooks: WebhookService
    payment_methods: PaymentMethodsService
    reconciliation: ReconciliationService
    owns_engine: bool = True
    owns_client: bool = True

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        engine: AsyncEngine | None = None,
        mp_client: MercadoPagoClient | None = None,
    ) -> Container:
        owns_engine = engine is None
        owns_client = mp_client is None
        engine = engine or create_engine(settings)
        sessionmaker = create_sessionmaker(engine)
        client = mp_client or MercadoPagoClient(settings)
        payments = PaymentService(sessionmaker, client, settings)
        refunds = RefundService(sessionmaker, client, payments, settings)
        return cls(
            settings=settings,
            engine=engine,
            sessionmaker=sessionmaker,
            mp_client=client,
            idempotency=IdempotencyService(sessionmaker, settings),
            orders=OrderService(sessionmaker, settings),
            payments=payments,
            checkout=CheckoutService(sessionmaker, client, payments, settings),
            refunds=refunds,
            webhooks=WebhookService(sessionmaker, client, payments, settings),
            payment_methods=PaymentMethodsService(client, settings),
            reconciliation=ReconciliationService(sessionmaker, payments, refunds, settings),
            owns_engine=owns_engine,
            owns_client=owns_client,
        )

    async def aclose(self) -> None:
        if self.owns_client:
            await self.mp_client.aclose()
        if self.owns_engine:
            await self.engine.dispose()
