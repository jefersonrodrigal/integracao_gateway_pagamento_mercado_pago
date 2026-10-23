"""Erros da integração com o Mercado Pago.

Classificação usada pelos serviços:
- `MercadoPagoAPIError` com `outcome_unknown=False`: o provedor respondeu de forma definitiva
  (ex.: 400 dados inválidos, 402 transação recusada, 409 regra de negócio).
- `MercadoPagoOutcomeUnknownError`: não sabemos se a operação foi efetivada (timeout, queda de
  conexão, 5xx, 423, chave idempotente já usada). Nunca se cria nova cobrança nesse caso:
  consulta-se a operação original (reconciliação).
"""

from __future__ import annotations

from typing import Any


class MercadoPagoError(Exception):
    def __init__(self, message: str, *, operation: str) -> None:
        super().__init__(message)
        self.operation = operation


class MercadoPagoAPIError(MercadoPagoError):
    def __init__(
        self,
        *,
        operation: str,
        status_code: int,
        code: str | None,
        message: str,
        payload: dict[str, Any] | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(
            f"{operation}: HTTP {status_code} {code or ''}".strip(), operation=operation
        )
        self.status_code = status_code
        self.code = code
        self.provider_message = message[:255]
        self.payload = payload
        self.provider_request_id = provider_request_id


class MercadoPagoOutcomeUnknownError(MercadoPagoError):
    def __init__(
        self,
        *,
        operation: str,
        reason: str,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(f"{operation}: resultado desconhecido ({reason})", operation=operation)
        self.reason = reason
        self.status_code = status_code
        self.code = code


class MercadoPagoUnavailableError(MercadoPagoError):
    """Falha transitória em operação de leitura (seguro tentar depois)."""
