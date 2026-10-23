from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.domain.states import EventStatus


class WebhookData(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str | None = None


class MercadoPagoNotification(BaseModel):
    """Corpo da notificação. Informativo apenas: o estado real é sempre consultado na API."""

    model_config = ConfigDict(extra="allow")

    action: str | None = None
    type: str | None = None
    api_version: str | None = None
    live_mode: bool | None = None
    data: WebhookData | None = None


class WebhookAck(BaseModel):
    status: Literal["received", "duplicate", "ignored"]
    event_id: uuid.UUID | None = None


class PaymentEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    action: str | None
    resource_id: str | None
    status: EventStatus
    result: str | None
    attempts: int
    duplicate_count: int
    last_error: str | None
    received_at: datetime
    processed_at: datetime | None
