from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, str_enum, utcnow
from app.domain.states import EventStatus


class PaymentEvent(UUIDPrimaryKeyMixin, Base):
    """Notificação recebida do provedor. Funciona também como fila durável de processamento."""

    __tablename__ = "payment_events"
    __table_args__ = (Index("ix_payment_events_queue", "status", "next_attempt_at"),)

    provider: Mapped[str] = mapped_column(String(32), default="mercadopago")
    # Hash de (tipo, recurso, ação, corpo): reenvios idênticos do provedor são deduplicados.
    dedup_key: Mapped[str] = mapped_column(String(64), unique=True)
    external_event_id: Mapped[str | None] = mapped_column(String(128))  # x-request-id
    event_type: Mapped[str] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"), index=True
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[EventStatus] = mapped_column(
        str_enum(EventStatus, "event_status"), default=EventStatus.PENDING
    )
    result: Mapped[str | None] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(default=0)
    duplicate_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    next_attempt_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    locked_until: Mapped[datetime | None]
    received_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    processed_at: Mapped[datetime | None]
