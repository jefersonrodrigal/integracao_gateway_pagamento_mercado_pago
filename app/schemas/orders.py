from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator

from app.domain.states import OrderStatus
from app.schemas.common import APIModel, Money

Sku = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._\-]{1,64}$")]


class OrderItemCreate(APIModel):
    sku: Sku
    quantity: int = Field(ge=1, le=1000)


class OrderCreate(APIModel):
    """Somente SKUs e quantidades: preços, descontos e frete são calculados no servidor."""

    items: list[OrderItemCreate] = Field(min_length=1, max_length=50)
    coupon_code: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_\-]{3,32}$")] | None = None
    reference: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:\-]{1,64}$")] | None = None

    @field_validator("items")
    @classmethod
    def _unique_skus(cls, items: list[OrderItemCreate]) -> list[OrderItemCreate]:
        skus = [item.sku for item in items]
        if len(skus) != len(set(skus)):
            raise ValueError("SKUs duplicados: agrupe as quantidades em um único item")
        return items


class OrderItemOut(APIModel):
    sku: str
    name: str
    unit_price: Money
    quantity: int
    line_total: Money


class OrderOut(APIModel):
    id: uuid.UUID
    customer_id: str
    reference: str | None
    status: OrderStatus
    currency: str
    subtotal: Money
    discount_total: Money
    shipping_total: Money
    total_amount: Money
    coupon_code: str | None
    items: list[OrderItemOut]
    created_at: datetime
    updated_at: datetime
    paid_at: datetime | None
    cancelled_at: datetime | None
