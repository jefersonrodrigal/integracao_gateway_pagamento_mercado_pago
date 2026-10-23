from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.states import EventStatus
from app.models.payment_event import PaymentEvent


class PaymentEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_or_count_duplicate(self, values: dict[str, Any]) -> tuple[uuid.UUID, bool]:
        """Insere o evento; se a `dedup_key` já existe, incrementa o contador de duplicatas.

        Retorna (id, inserted).
        """
        base = insert(PaymentEvent).values(id=uuid.uuid4(), **values)
        stmt = base.on_conflict_do_update(
            index_elements=[PaymentEvent.dedup_key],
            set_={"duplicate_count": PaymentEvent.duplicate_count + 1},
        ).returning(PaymentEvent.id, PaymentEvent.duplicate_count)
        row = (await self.session.execute(stmt)).one()
        return row.id, row.duplicate_count == 0

    async def get(self, event_id: uuid.UUID, *, for_update: bool = False) -> PaymentEvent | None:
        stmt = select(PaymentEvent).where(PaymentEvent.id == event_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def claim(
        self, *, now: datetime, lock_for: timedelta, limit: int, event_id: uuid.UUID | None = None
    ) -> list[PaymentEvent]:
        """Reserva eventos prontos para processamento (seguro entre múltiplos workers)."""
        ready = or_(
            PaymentEvent.status.in_([EventStatus.PENDING, EventStatus.FAILED])
            & (PaymentEvent.next_attempt_at <= now),
            (PaymentEvent.status == EventStatus.PROCESSING) & (PaymentEvent.locked_until <= now),
        )
        stmt = select(PaymentEvent).where(ready)
        if event_id is not None:
            stmt = stmt.where(PaymentEvent.id == event_id)
        stmt = (
            stmt.order_by(PaymentEvent.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        events = list((await self.session.execute(stmt)).scalars())
        for event in events:
            event.status = EventStatus.PROCESSING
            event.locked_until = now + lock_for
            event.attempts += 1
        return events

    async def purge_payloads(self, older_than: datetime) -> int:
        """Retenção (LGPD): remove o corpo bruto de eventos antigos, preservando o registro."""
        stmt = (
            update(PaymentEvent)
            .where(
                PaymentEvent.received_at < older_than,
                PaymentEvent.payload.is_not(None),
                PaymentEvent.status.in_([EventStatus.PROCESSED, EventStatus.IGNORED]),
            )
            .values(payload=None)
        )
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
