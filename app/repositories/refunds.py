from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.states import RefundStatus
from app.models.refund import Refund


class RefundRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, refund: Refund) -> None:
        self.session.add(refund)

    async def get(self, refund_id: uuid.UUID, *, for_update: bool = False) -> Refund | None:
        stmt = select(Refund).where(Refund.id == refund_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def committed_amount(self, payment_id: uuid.UUID) -> Decimal:
        """Soma de reembolsos aprovados ou em andamento (reserva o saldo reembolsável)."""
        stmt = select(func.coalesce(func.sum(Refund.amount), 0)).where(
            Refund.payment_id == payment_id,
            Refund.status.in_([RefundStatus.PENDING, RefundStatus.APPROVED]),
        )
        return Decimal((await self.session.execute(stmt)).scalar_one())

    async def approved_amount(self, payment_id: uuid.UUID) -> Decimal:
        stmt = select(func.coalesce(func.sum(Refund.amount), 0)).where(
            Refund.payment_id == payment_id, Refund.status == RefundStatus.APPROVED
        )
        return Decimal((await self.session.execute(stmt)).scalar_one())

    async def list_for_payment(self, payment_id: uuid.UUID) -> list[Refund]:
        stmt = select(Refund).where(Refund.payment_id == payment_id).order_by(Refund.created_at)
        return list((await self.session.execute(stmt)).scalars())

    async def known_provider_ids(self, payment_id: uuid.UUID) -> set[str]:
        stmt = select(Refund.provider_refund_id).where(
            Refund.payment_id == payment_id, Refund.provider_refund_id.is_not(None)
        )
        return {row for row in (await self.session.execute(stmt)).scalars() if row}

    async def list_pending(self, *, stale_before: datetime, limit: int) -> list[Refund]:
        stmt = (
            select(Refund)
            .where(
                Refund.status == RefundStatus.PENDING,
                Refund.updated_at <= stale_before,
                Refund.last_reconciled_at.is_(None) | (Refund.last_reconciled_at <= stale_before),
            )
            .order_by(Refund.updated_at)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())
