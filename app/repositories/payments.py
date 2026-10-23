from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.states import ACTIVE_PAYMENT_STATUSES, SETTLED_PAYMENT_STATUSES, PaymentStatus
from app.models.payment import Payment


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, payment: Payment) -> None:
        self.session.add(payment)

    async def get(self, payment_id: uuid.UUID, *, for_update: bool = False) -> Payment | None:
        stmt = select(Payment).where(Payment.id == payment_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_active_for_order(
        self, order_id: uuid.UUID, *, for_update: bool = False
    ) -> Payment | None:
        stmt = select(Payment).where(
            Payment.order_id == order_id, Payment.status.in_(ACTIVE_PAYMENT_STATUSES)
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_settled_for_order(
        self, order_id: uuid.UUID, *, for_update: bool = False
    ) -> Payment | None:
        stmt = (
            select(Payment)
            .where(Payment.order_id == order_id, Payment.status.in_(SETTLED_PAYMENT_STATUSES))
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_order(self, order_id: uuid.UUID) -> list[Payment]:
        stmt = select(Payment).where(Payment.order_id == order_id).order_by(Payment.created_at)
        return list((await self.session.execute(stmt)).scalars())

    async def find_by_provider_reference(
        self, *, provider_order_id: str | None, external_reference: str | None
    ) -> Payment | None:
        conditions = []
        if provider_order_id:
            conditions.append(Payment.provider_order_id == provider_order_id)
        if external_reference:
            conditions.append(Payment.external_reference == external_reference)
        if not conditions:
            return None
        stmt = select(Payment).where(or_(*conditions)).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_reconciliation(
        self, *, stale_before: datetime, unknown_before: datetime, limit: int
    ) -> list[Payment]:
        """Pagamentos com resultado desconhecido ou pendentes sem atualização recente."""
        stmt = (
            select(Payment)
            .where(
                or_(
                    (Payment.status == PaymentStatus.UNKNOWN)
                    & (Payment.updated_at <= unknown_before),
                    Payment.status.in_(ACTIVE_PAYMENT_STATUSES - {PaymentStatus.UNKNOWN})
                    & (Payment.updated_at <= stale_before)
                    & (
                        (Payment.last_reconciled_at.is_(None))
                        | (Payment.last_reconciled_at <= stale_before)
                    ),
                )
            )
            .order_by(Payment.updated_at)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())
