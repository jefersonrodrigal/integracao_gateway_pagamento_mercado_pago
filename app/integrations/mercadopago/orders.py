"""Montagem do corpo de `POST /v1/orders` por meio de pagamento.

Contratos conforme a documentação oficial (Checkout API via Orders):
- Pix:    payment_method {id: "pix", type: "bank_transfer"}, expiration_time ISO 8601.
- Boleto: payment_method {id: "boleto", type: "ticket"}, payer com identificação e endereço.
- Cartão: payment_method {id: <bandeira>, type: credit_card|debit_card, token, installments};
          capture_mode "manual" reserva o valor para captura posterior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.core.money import format_amount
from app.domain.states import PaymentMethod

PROVIDER_METHOD = {
    PaymentMethod.PIX: ("pix", "bank_transfer"),
    PaymentMethod.BOLETO: ("boleto", "ticket"),
}


@dataclass(frozen=True, slots=True)
class OrderPaymentInput:
    external_reference: str
    amount: Decimal
    method: PaymentMethod
    payment_method_id: str
    payer: dict[str, Any]
    description: str | None = None
    card_token: str | None = field(default=None, repr=False)
    installments: int | None = None
    capture_manual: bool = False
    expiration_time: str | None = None
    statement_descriptor: str | None = None


def build_create_order_payload(data: OrderPaymentInput) -> dict[str, Any]:
    amount = format_amount(data.amount)
    payment_method: dict[str, Any]
    if data.method in (PaymentMethod.CREDIT_CARD, PaymentMethod.DEBIT_CARD):
        if not data.card_token:
            raise ValueError("token do cartão é obrigatório")
        payment_method = {
            "id": data.payment_method_id,
            "type": data.method.value,
            "token": data.card_token,
            "installments": data.installments or 1,
        }
        if data.statement_descriptor:
            payment_method["statement_descriptor"] = data.statement_descriptor
    else:
        method_id, method_type = PROVIDER_METHOD[data.method]
        payment_method = {"id": method_id, "type": method_type}

    payment: dict[str, Any] = {"amount": amount, "payment_method": payment_method}
    if data.expiration_time and data.method in PROVIDER_METHOD:
        payment["expiration_time"] = data.expiration_time

    payload: dict[str, Any] = {
        "type": "online",
        "processing_mode": "automatic",
        "external_reference": data.external_reference,
        "total_amount": amount,
        "payer": data.payer,
        "transactions": {"payments": [payment]},
    }
    if data.capture_manual:
        payload["capture_mode"] = "manual"
    if data.description:
        payload["description"] = data.description[:255]
    return payload
