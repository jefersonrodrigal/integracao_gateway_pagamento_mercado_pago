from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class Product(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Catálogo: fonte confiável de preço e estoque para o cálculo do pedido."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("unit_price > 0", name="unit_price_positive"),
        CheckConstraint("stock >= 0", name="stock_non_negative"),
    )

    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    unit_price: Mapped[Decimal]
    stock: Mapped[int]
    active: Mapped[bool] = mapped_column(default=True)


class CouponKind(StrEnum):
    PERCENT = "percent"
    FIXED = "fixed"


class Coupon(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "coupons"
    __table_args__ = (
        CheckConstraint("value > 0", name="value_positive"),
        CheckConstraint("kind <> 'percent' OR value <= 100", name="percent_max_100"),
        CheckConstraint("used_count >= 0", name="used_count_non_negative"),
    )

    code: Mapped[str] = mapped_column(String(32), unique=True)
    kind: Mapped[CouponKind] = mapped_column(str_enum(CouponKind, "coupon_kind"))
    value: Mapped[Decimal]
    active: Mapped[bool] = mapped_column(default=True)
    min_subtotal: Mapped[Decimal | None]
    valid_from: Mapped[datetime | None]
    valid_until: Mapped[datetime | None]
    max_uses: Mapped[int | None]
    used_count: Mapped[int] = mapped_column(default=0)
