from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class StateTransition(Base):
    """Trilha de auditoria append-only das mudanças de estado.

    Também serve de outbox: consumidores de fulfillment podem ler transições para `paid`
    em ordem de `id`, garantindo liberação única de produtos/benefícios.
    """

    __tablename__ = "state_transitions"
    __table_args__ = (Index("ix_state_transitions_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(16))  # order | payment | refund
    entity_id: Mapped[uuid.UUID]
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))  # api | webhook | reconciliation | provider
    reason: Mapped[str | None] = mapped_column(String(255))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
