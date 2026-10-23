from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.domain.states import ACTIVE_PAYMENT_STATUSES, PaymentMethod, PaymentStatus
from app.models.order import Order

_ACTIVE_SQL = ", ".join(f"'{status.value}'" for status in sorted(ACTIVE_PAYMENT_STATUSES))


class Payment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Uma tentativa de pagamento. Cada tentativa corresponde a uma order no Mercado Pago."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("refunded_amount >= 0", name="refunded_non_negative"),
        CheckConstraint("refunded_amount <= amount", name="refunded_not_above_amount"),
        CheckConstraint("installments >= 1", name="installments_positive"),
        # Garantia no banco: no máximo uma tentativa ativa por pedido.
        Index(
            "uq_payments_one_active_per_order",
            "order_id",
            unique=True,
            postgresql_where=text(f"status IN ({_ACTIVE_SQL})"),
        ),
        Index("ix_payments_status_reconciled", "status", "last_reconciled_at"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[PaymentStatus] = mapped_column(
        str_enum(PaymentStatus, "payment_status"), default=PaymentStatus.CREATED
    )
    method: Mapped[PaymentMethod] = mapped_column(str_enum(PaymentMethod, "payment_method"))
    payment_method_id: Mapped[str] = mapped_column(String(32))  # ex.: pix, boleto, master, visa
    amount: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3))
    installments: Mapped[int] = mapped_column(default=1)
    capture_mode: Mapped[str] = mapped_column(String(16), default="automatic")

    # Referências da operação
    external_reference: Mapped[str] = mapped_column(String(64), unique=True)
    provider_idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    client_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    provider_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    provider_status: Mapped[str | None] = mapped_column(String(32))
    provider_status_detail: Mapped[str | None] = mapped_column(String(64))
    provider_updated_at: Mapped[datetime | None]

    paid_amount: Mapped[Decimal | None]
    refunded_amount: Mapped[Decimal] = mapped_column(default=Decimal("0.00"))
    # Dados públicos (QR Code Pix, linha digitável/URL do boleto). Nunca dados de cartão.
    payment_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None]

    create_attempts: Mapped[int] = mapped_column(default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(255))
    last_reconciled_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(default=1)

    order: Mapped[Order] = relationship(back_populates="payments", lazy="raise")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
