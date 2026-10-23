"""Modelos tipados das respostas da Orders API.

Somente campos documentados na referência oficial são mapeados; campos extras são
ignorados para que mudanças compatíveis do provedor não quebrem a integração.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _MPModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class MPPaymentMethod(_MPModel):
    id: str | None = None
    type: str | None = None
    installments: int | None = None
    ticket_url: str | None = None
    qr_code: str | None = None
    qr_code_base64: str | None = None
    barcode_content: str | None = None
    digitable_line: str | None = None
    reference: str | None = None
    verification_code: str | None = None
    financial_institution: str | None = None


class MPPayment(_MPModel):
    id: str
    amount: Decimal | None = None
    paid_amount: Decimal | None = None
    reference_id: str | None = None
    status: str | None = None
    status_detail: str | None = None
    date_of_expiration: datetime | None = None
    payment_method: MPPaymentMethod = Field(default_factory=MPPaymentMethod)


class MPRefund(_MPModel):
    id: str
    transaction_id: str | None = None
    reference_id: str | None = None
    amount: Decimal | None = None
    status: str | None = None


class MPTransactions(_MPModel):
    payments: list[MPPayment] = Field(default_factory=list)
    refunds: list[MPRefund] = Field(default_factory=list)


class MPOrder(_MPModel):
    id: str
    type: str | None = None
    status: str | None = None
    status_detail: str | None = None
    external_reference: str | None = None
    total_amount: Decimal | None = None
    total_paid_amount: Decimal | None = None
    capture_mode: str | None = None
    processing_mode: str | None = None
    created_date: datetime | None = None
    last_updated_date: datetime | None = None
    transactions: MPTransactions = Field(default_factory=MPTransactions)

    @property
    def payment(self) -> MPPayment | None:
        # A Orders API aceita uma única transação de pagamento por order.
        return self.transactions.payments[0] if self.transactions.payments else None


class MPOrderSearchResult(_MPModel):
    data: list[MPOrder] = Field(default_factory=list)


class MPPaymentMethodInfo(_MPModel):
    """Item de GET /v1/payment_methods."""

    id: str
    name: str | None = None
    payment_type_id: str | None = None
    status: str | None = None
    secure_thumbnail: str | None = None
    min_allowed_amount: Decimal | None = None
    max_allowed_amount: Decimal | None = None
