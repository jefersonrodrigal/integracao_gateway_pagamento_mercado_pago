"""Meios de pagamento disponíveis, derivados de `GET /v1/payment_methods` (com cache)."""

from __future__ import annotations

import asyncio
import time

from app.core.config import Settings
from app.core.exceptions import ServiceUnavailableError
from app.domain.states import PaymentMethod
from app.integrations.mercadopago.client import MercadoPagoClient
from app.integrations.mercadopago.exceptions import MercadoPagoError
from app.integrations.mercadopago.schemas import MPPaymentMethodInfo
from app.schemas.payment_methods import CardBrand, PaymentMethodOut, PaymentMethodsOut


def build_payment_methods(
    methods: list[MPPaymentMethodInfo], settings: Settings
) -> PaymentMethodsOut:
    """Converte o catálogo da conta nos meios suportados por esta integração.

    - Pix: disponível se a conta possui o meio `pix` ativo (payment_type_id bank_transfer).
    - Boleto: disponível se há meio ativo do tipo `ticket` (na Orders API o id enviado é `boleto`).
    - Cartões: bandeiras ativas de `credit_card` / `debit_card`.
    """
    active = [m for m in methods if (m.status or "").lower() == "active"]

    def limits(items: list[MPPaymentMethodInfo]) -> tuple[object, object]:
        mins = [m.min_allowed_amount for m in items if m.min_allowed_amount is not None]
        maxs = [m.max_allowed_amount for m in items if m.max_allowed_amount is not None]
        return (min(mins) if mins else None), (max(maxs) if maxs else None)

    pix = [m for m in active if m.id == "pix"]
    tickets = [m for m in active if m.payment_type_id == "ticket"]
    result: list[PaymentMethodOut] = []
    for method, items in ((PaymentMethod.PIX, pix), (PaymentMethod.BOLETO, tickets)):
        low, high = limits(items)
        result.append(
            PaymentMethodOut.model_validate(
                {"method": method, "available": bool(items), "min_amount": low, "max_amount": high}
            )
        )
    for method in (PaymentMethod.CREDIT_CARD, PaymentMethod.DEBIT_CARD):
        brands = [m for m in active if m.payment_type_id == method.value]
        low, high = limits(brands)
        result.append(
            PaymentMethodOut.model_validate(
                {
                    "method": method,
                    "available": bool(brands),
                    "min_amount": low,
                    "max_amount": high,
                    "brands": [
                        CardBrand(payment_method_id=b.id, name=b.name, thumbnail=b.secure_thumbnail)
                        for b in brands
                    ],
                    "max_installments": settings.max_installments
                    if method == PaymentMethod.CREDIT_CARD
                    else 1,
                }
            )
        )
    return PaymentMethodsOut(currency=settings.currency, methods=result)


class PaymentMethodsService:
    def __init__(self, client: MercadoPagoClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._cache: tuple[float, PaymentMethodsOut] | None = None
        self._lock = asyncio.Lock()

    async def list_methods(self) -> PaymentMethodsOut:
        ttl = self._settings.mercado_pago_payment_methods_cache_seconds
        async with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < ttl:
                return self._cache[1]
            try:
                methods = await self._client.list_payment_methods()
            except MercadoPagoError:
                if self._cache:  # serve o último catálogo conhecido
                    return self._cache[1]
                raise ServiceUnavailableError(code="payment_provider_unavailable") from None
            result = build_payment_methods(methods, self._settings)
            self._cache = (time.monotonic(), result)
            return result
