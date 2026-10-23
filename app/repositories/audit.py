from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import StateTransition


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def record(
        self,
        *,
        entity_type: str,
        entity_id: uuid.UUID,
        from_status: str | None,
        to_status: str,
        source: str,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            StateTransition(
                entity_type=entity_type,
                entity_id=entity_id,
                from_status=from_status,
                to_status=to_status,
                source=source,
                reason=reason[:255] if reason else None,
                details=details,
            )
        )
