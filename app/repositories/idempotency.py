from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.states import IdempotencyStatus
from app.models.idempotency import IdempotencyRecord


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def try_insert(self, values: dict[str, Any]) -> bool:
        stmt = (
            insert(IdempotencyRecord)
            .values(id=uuid.uuid4(), status=IdempotencyStatus.IN_PROGRESS, **values)
            .on_conflict_do_nothing(constraint="uq_idempotency_principal_scope_key")
            .returning(IdempotencyRecord.id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def get_for_update(
        self, principal: str, scope: str, key: str
    ) -> IdempotencyRecord | None:
        stmt = (
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.principal == principal,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.key == key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def delete(self, principal: str, scope: str, key: str) -> None:
        await self.session.execute(
            delete(IdempotencyRecord).where(
                IdempotencyRecord.principal == principal,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.key == key,
            )
        )

    async def purge_expired(self, now: datetime) -> int:
        result = await self.session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < now)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
