"""Idempotência interna para operações críticas (cabeçalho `Idempotency-Key`).

- Escopo: (identidade autenticada, operação, chave) — único no PostgreSQL.
- O hash canônico dos parâmetros relevantes (incluindo IDs de rota) é comparado a cada
  repetição; reutilizar a chave com parâmetros diferentes retorna 422.
- Requisição repetida após conclusão: devolve a resposta original (`Idempotent-Replayed: true`).
- Requisição repetida em andamento: 409; após `IDEMPOTENCY_LOCK_TIMEOUT_SECONDS` o registro
  pode ser retomado (os serviços são seguros para retomada).
- Erros 5xx/transitórios liberam a chave para nova tentativa; erros 4xx definitivos ficam
  armazenados para respostas determinísticas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import AppError, BusinessRuleError, ConflictError, error_response, require
from app.core.logging import log_fields
from app.core.security import Principal
from app.domain.states import IdempotencyStatus
from app.repositories.idempotency import IdempotencyRepository

logger = logging.getLogger(__name__)

KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")
# Erros que refletem estado transitório: não devem ser "congelados" na chave.
_TRANSIENT_ERROR_STATUS = frozenset({409, 423, 429})


def request_fingerprint(payload: Any) -> str:
    canonical = json.dumps(jsonable_encoder(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationResult:
    status_code: int
    body: BaseModel
    resource_type: str | None = None
    resource_id: str | None = None


class IdempotencyService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings

    async def execute(
        self,
        *,
        principal: Principal,
        scope: str,
        key: str | None,
        fingerprint_payload: Any,
        operation: Callable[[], Awaitable[OperationResult]],
    ) -> JSONResponse:
        if not key or not KEY_PATTERN.match(key):
            raise BusinessRuleError(
                "Cabeçalho Idempotency-Key ausente ou inválido (8-128 caracteres [A-Za-z0-9_-:.]).",
                code="invalid_idempotency_key",
            )
        request_hash = request_fingerprint(fingerprint_payload)
        replay = await self._begin(principal.subject, scope, key, request_hash)
        if replay is not None:
            return replay

        try:
            result = await operation()
        except AppError as exc:
            if exc.status_code >= 500 or exc.status_code in _TRANSIENT_ERROR_STATUS:
                await self._release(principal.subject, scope, key)
            else:
                body = error_response(exc.status_code, exc.code, exc.message, exc.details)
                await self._complete(
                    principal.subject, scope, key, exc.status_code, json.loads(bytes(body.body))
                )
            raise
        except BaseException:
            await self._release(principal.subject, scope, key)
            raise

        content = jsonable_encoder(result.body)
        await self._complete(
            principal.subject,
            scope,
            key,
            result.status_code,
            content,
            result.resource_type,
            result.resource_id,
        )
        return JSONResponse(status_code=result.status_code, content=content)

    async def _begin(
        self, principal: str, scope: str, key: str, request_hash: str
    ) -> JSONResponse | None:
        now = datetime.now(UTC)
        async with self._sessionmaker() as session, session.begin():
            repo = IdempotencyRepository(session)
            inserted = await repo.try_insert(
                {
                    "principal": principal,
                    "scope": scope,
                    "key": key,
                    "request_hash": request_hash,
                    "locked_at": now,
                    "expires_at": now + timedelta(hours=self._settings.idempotency_ttl_hours),
                }
            )
            if inserted:
                return None
            record = await repo.get_for_update(principal, scope, key)
            record = require(record, "record")
            if record.expires_at <= now:
                record.request_hash = request_hash
                record.status = IdempotencyStatus.IN_PROGRESS
                record.response_status_code = None
                record.response_body = None
                record.locked_at = now
                record.expires_at = now + timedelta(hours=self._settings.idempotency_ttl_hours)
                return None
            if record.request_hash != request_hash:
                raise BusinessRuleError(
                    "Idempotency-Key já utilizada com parâmetros diferentes.",
                    code="idempotency_key_reused",
                )
            if record.status == IdempotencyStatus.COMPLETED:
                logger.info("idempotent_replay", extra=log_fields(scope=scope))
                return JSONResponse(
                    status_code=record.response_status_code or 200,
                    content=record.response_body,
                    headers={"Idempotent-Replayed": "true"},
                )
            lock_timeout = timedelta(seconds=self._settings.idempotency_lock_timeout_seconds)
            if now - record.locked_at < lock_timeout:
                raise ConflictError(
                    "Requisição com esta Idempotency-Key ainda em processamento.",
                    code="idempotency_request_in_progress",
                    headers={"Retry-After": "2"},
                )
            record.locked_at = now  # retomada após falha do processo anterior
            return None

    async def _complete(  # noqa: PLR0917
        self,
        principal: str,
        scope: str,
        key: str,
        status_code: int,
        body: Any,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            record = await IdempotencyRepository(session).get_for_update(principal, scope, key)
            if record is None:
                return
            record.status = IdempotencyStatus.COMPLETED
            record.response_status_code = status_code
            record.response_body = body
            record.resource_type = resource_type
            record.resource_id = resource_id

    async def _release(self, principal: str, scope: str, key: str) -> None:
        try:
            async with self._sessionmaker() as session, session.begin():
                await IdempotencyRepository(session).delete(principal, scope, key)
        except Exception:
            logger.exception("idempotency_release_failed", extra=log_fields(scope=scope))
