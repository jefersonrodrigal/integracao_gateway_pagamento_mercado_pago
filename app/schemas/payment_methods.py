from __future__ import annotations

from pydantic import Field

from app.domain.states import PaymentMethod
from app.schemas.common import APIModel, Money


class CardBrand(APIModel):
    payment_method_id: str
    name: str | None
    thumbnail: str | None


class PaymentMethodOut(APIModel):
    method: PaymentMethod
    available: bool
    min_amount: Money | None = None
    max_amount: Money | None = None
    brands: list[CardBrand] = Field(default_factory=list)
    max_installments: int | None = None


class PaymentMethodsOut(APIModel):
    currency: str
    methods: list[PaymentMethodOut]
