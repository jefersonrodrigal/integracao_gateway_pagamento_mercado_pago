"""Erros de domínio e handlers HTTP com respostas seguras e padronizadas.

Formato de erro:
    {"error": {"code": "...", "message": "...", "details": {...}, "request_id": "..."}}
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import log_fields, request_id_ctx

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 400
    code = "bad_request"
    message = "Requisição inválida."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        self.headers = headers
        super().__init__(self.message)


class BusinessRuleError(AppError):
    status_code = 422
    code = "business_rule_violation"
    message = "A operação viola uma regra de negócio."


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    message = "Recurso não encontrado."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    message = "A operação conflita com o estado atual do recurso."


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthenticated"
    message = "Credenciais ausentes ou inválidas."

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(message, **kwargs)


class PermissionDeniedError(AppError):
    status_code = 403
    code = "forbidden"
    message = "Permissão insuficiente para esta operação."


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Limite de requisições excedido."


class PaymentProviderError(AppError):
    """Falha definitiva ou indisponibilidade do provedor de pagamento."""

    status_code = 502
    code = "payment_provider_error"
    message = "Falha na comunicação com o provedor de pagamento."


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
    message = "Serviço temporariamente indisponível. Tente novamente."


def _error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details:
        body["details"] = details
    request_id = request_id_ctx.get()
    if request_id:
        body["request_id"] = request_id
    return {"error": body}


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=_error_body(code, message, details), headers=headers
    )


async def _app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(AppError, exc)
    if exc.status_code >= 500:
        logger.warning("app_error", extra=log_fields(code=exc.code, status=exc.status_code))
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def _validation_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, exc)
    # Nunca devolve o valor recebido: pode conter token de cartão, CPF ou e-mail.
    errors = [
        {
            "loc": [str(part) for part in err.get("loc", ())],
            "msg": err.get("msg"),
            "type": err.get("type"),
        }
        for err in exc.errors()
    ]
    return error_response(
        422, "validation_error", "Dados de entrada inválidos.", {"errors": errors}
    )


async def _http_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, exc)
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else "Erro HTTP."
    return error_response(exc.status_code, code, message, headers=getattr(exc, "headers", None))


async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_exception", exc_info=exc)
    return error_response(500, "internal_error", "Erro interno inesperado.")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_handler)


class InvariantViolationError(RuntimeError):
    """Estado interno impossível (bug ou corrupção): aborta a transação corrente."""


def require[T](value: T | None, what: str = "valor") -> T:
    if value is None:
        raise InvariantViolationError(f"{what} ausente")
    return value
