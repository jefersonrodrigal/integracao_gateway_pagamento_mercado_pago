from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# Valores monetários trafegam como string decimal com 2 casas ("123.45").
Money = Annotated[
    Decimal,
    Field(max_digits=12, decimal_places=2, examples=["123.45"]),
    PlainSerializer(lambda value: f"{value:.2f}", return_type=str, when_used="json"),
]


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    descriptions = {
        400: "Requisição inválida",
        401: "Não autenticado",
        403: "Sem permissão",
        404: "Recurso não encontrado (ou não pertence ao usuário)",
        409: "Conflito com o estado atual / requisição idempotente em andamento",
        422: "Dados inválidos, regra de negócio violada ou chave idempotente reutilizada",
        429: "Limite de requisições excedido",
        502: "Falha do provedor de pagamento",
        503: "Serviço/provedor temporariamente indisponível",
    }
    return {code: {"model": ErrorResponse, "description": descriptions[code]} for code in codes}
