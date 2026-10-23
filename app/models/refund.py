from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.domain.states import RefundStatus


class Refund(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        # Um reembolso em andamento por pagamento: evita somas concorrentes acima do pago.
        Index(
            "uq_refunds_one_pending_per_payment",
            "payment_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[RefundStatus] = mapped_column(
        str_enum(RefundStatus, "refund_status"), default=RefundStatus.PENDING
    )
    amount: Mapped[Decimal]
    is_full: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    requested_by: Mapped[str] = mapped_column(String(64))
    provider_refund_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    provider_status: Mapped[str | None] = mapped_column(String(32))
    provider_idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    client_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(255))
    processed_at: Mapped[datetime | None]
    last_reconciled_at: Mapped[datetime | None]
