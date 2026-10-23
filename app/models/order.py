from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.domain.states import OrderStatus

if TYPE_CHECKING:
    from app.models.payment import Payment


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("discount_total >= 0", name="discount_non_negative"),
        CheckConstraint("shipping_total >= 0", name="shipping_non_negative"),
        CheckConstraint("total_amount > 0", name="total_positive"),
        CheckConstraint(
            "total_amount = subtotal - discount_total + shipping_total", name="total_consistent"
        ),
        Index("ix_orders_customer_created", "customer_id", "created_at"),
    )

    customer_id: Mapped[str] = mapped_column(String(64))
    reference: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[OrderStatus] = mapped_column(
        str_enum(OrderStatus, "order_status"), default=OrderStatus.PENDING_PAYMENT
    )
    currency: Mapped[str] = mapped_column(String(3))
    subtotal: Mapped[Decimal]
    discount_total: Mapped[Decimal]
    shipping_total: Mapped[Decimal]
    total_amount: Mapped[Decimal]
    coupon_code: Mapped[str | None] = mapped_column(String(32))
    paid_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(default=1)

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", lazy="selectin", order_by="OrderItem.sku"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="order", lazy="raise", order_by="Payment.created_at"
    )

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class OrderItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price > 0", name="unit_price_positive"),
        CheckConstraint("line_total = unit_price * quantity", name="line_total_consistent"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    unit_price: Mapped[Decimal]
    quantity: Mapped[int]
    line_total: Mapped[Decimal]

    order: Mapped[Order] = relationship(back_populates="items")
