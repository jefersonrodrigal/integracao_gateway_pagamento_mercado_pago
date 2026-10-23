from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request

from app.api.deps import ContainerDep, PrincipalDep
from app.container import Container
from app.core.logging import log_fields
from app.schemas.common import error_responses
from app.schemas.webhooks import MercadoPagoNotification, PaymentEventOut, WebhookAck

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


@router.post(
    "/mercadopago",
    response_model=WebhookAck,
    responses=error_responses(401, 429),
    summary="Receber notificações do Mercado Pago",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": MercadoPagoNotification.model_json_schema()}}
        }
    },
)
async def receive_mercadopago_notification(  # noqa: PLR0917
    request: Request,
    container: ContainerDep,
    background: BackgroundTasks,
    x_signature: Annotated[str | None, Header(alias="x-signature")] = None,
    x_request_id: Annotated[str | None, Header(alias="x-request-id")] = None,
    data_id: Annotated[str | None, Query(alias="data.id", max_length=64)] = None,
    topic: Annotated[str | None, Query(alias="type", max_length=64)] = None,
) -> WebhookAck:
    """Endpoint público para Webhooks do Mercado Pago (tópico `order`).

    - Valida `x-signature` (HMAC-SHA256 do manifesto `id;request-id;ts`) com
      `MERCADO_PAGO_WEBHOOK_SECRET`. Assinatura inválida → 401.
    - Persiste o evento (deduplicado por conteúdo) e responde 200 imediatamente.
    - O estado real é obtido consultando `GET /v1/orders/{data.id}`; o corpo da
      notificação nunca é usado como prova de pagamento.
    - Processamento em background e/ou pelo worker durável, com retries e dead letter.
    """
    raw_body = await request.body()
    outcome, event_id = await container.webhooks.receive(
        raw_body=raw_body,
        x_signature=x_signature,
        x_request_id=x_request_id,
        query_data_id=data_id,
        query_type=topic,
    )
    if outcome == "received" and event_id and container.settings.webhook_process_inline:
        background.add_task(_process_quietly, container, event_id)
    return WebhookAck(status=outcome, event_id=event_id)


async def _process_quietly(container: Container, event_id: uuid.UUID) -> None:
    try:
        await container.webhooks.process_event(event_id)
    except Exception:
        # O evento permanece reservado/pendente; o worker o processará.
        logger.exception(
            "inline_webhook_processing_failed", extra=log_fields(event_id=str(event_id))
        )


@router.post(
    "/events/{event_id}/reprocess",
    response_model=PaymentEventOut,
    responses=error_responses(401, 403, 404, 429),
    summary="Reprocessar evento (admin)",
)
async def reprocess_event(
    event_id: uuid.UUID, container: ContainerDep, principal: PrincipalDep
) -> PaymentEventOut:
    """Reenfileira e processa novamente um evento (ex.: `dead`). Seguro: o processamento
    consulta o estado atual no provedor e respeita as regras de transição."""
    event = await container.webhooks.reprocess(principal, event_id)
    return PaymentEventOut.model_validate(event)
