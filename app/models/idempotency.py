from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin, str_enum, utcnow
from app.domain.states import IdempotencyStatus


class IdempotencyRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("principal", "scope", "key", name="uq_idempotency_principal_scope_key"),
        Index("ix_idempotency_records_expires_at", "expires_at"),
    )

    principal: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[IdempotencyStatus] = mapped_column(
        str_enum(IdempotencyStatus, "idempotency_status"), default=IdempotencyStatus.IN_PROGRESS
    )
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    response_status_code: Mapped[int | None]
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    locked_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    expires_at: Mapped[datetime]
