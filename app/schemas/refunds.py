from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import Field

from app.domain.states import RefundStatus
from app.schemas.common import APIModel, Money


class RefundCreate(APIModel):
    amount: Money | None = Field(
        default=None,
        gt=Decimal("0"),
        description="Valor a reembolsar. Omitido = reembolso total do saldo restante.",
    )
    reason: str | None = Field(default=None, max_length=255)


class RefundOut(APIModel):
    id: uuid.UUID
    payment_id: uuid.UUID
    order_id: uuid.UUID
    status: RefundStatus
    amount: Money
    is_full: bool
    reason: str | None
    provider_refund_id: str | None
    provider_status: str | None
    created_at: datetime
    processed_at: datetime | None
