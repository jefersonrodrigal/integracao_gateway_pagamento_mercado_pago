from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import ContainerDep, IdempotencyKey, PrincipalDep
from app.domain.states import ACTIVE_PAYMENT_STATUSES
from app.schemas.common import error_responses
from app.schemas.payments import PaymentCreate, PaymentOut, PaymentStatusOut
from app.services.idempotency_service import OperationResult

router = APIRouter(prefix="/orders", tags=["payments"])

_EXAMPLES = {
    "pix": {
        "summary": "Pix",
        "value": {"method": "pix", "payer": {"email": "test_user_br@testuser.com"}},
    },
    "credit_card": {
        "summary": "Cartão de crédito (token gerado no front-end)",
        "value": {
            "method": "credit_card",
            "token": "<CARD_TOKEN>",
            "payment_method_id": "master",
            "installments": 1,
            "capture": True,
            "payer": {
                "email": "test_user_br@testuser.com",
                "identification": {"type": "CPF", "number": "19119119100"},
            },
        },
    },
    "boleto": {
        "summary": "Boleto",
        "value": {
            "method": "boleto",
            "payer": {
                "email": "test_user_br@testuser.com",
                "first_name": "APRO",
                "last_name": "Teste",
                "identification": {"type": "CPF", "number": "19119119100"},
                "address": {
                    "zip_code": "06233903",
                    "street_name": "Av. das Nações Unidas",
                    "street_number": "3003",
                    "neighborhood": "Bonfim",
                    "city": "Osasco",
                    "state": "SP",
                },
            },
        },
    },
}


@router.post(
    "/{order_id}/payments",
    status_code=status.HTTP_201_CREATED,
    response_model=PaymentOut,
    responses=error_responses(401, 403, 404, 409, 422, 429, 502, 503),
    summary="Iniciar pagamento",
)
async def start_payment(
    order_id: uuid.UUID,
    payload: Annotated[PaymentCreate, Body(openapi_examples=_EXAMPLES)],
    container: ContainerDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    """Inicia uma tentativa de pagamento (Pix, cartão de crédito/débito ou boleto).

    **Regras de negócio**
    - O pedido deve pertencer ao cliente e estar em `pending_payment`; o valor cobrado é
      sempre o `total_amount` calculado no servidor.
    - Apenas uma tentativa ativa por pedido (garantido por índice único parcial e lock).
    - Cria uma order no Mercado Pago (`POST /v1/orders`, `processing_mode=automatic`) com
      `X-Idempotency-Key` persistido; retries reutilizam a mesma chave.
    - Cartão: aceita apenas o **token** gerado pelo MercadoPago.js/Brick (nunca PAN/CVV).
      `capture=false` reserva o valor (`authorized`) para captura posterior.
    - Pix/boleto: retorna `pending` com QR Code / linha digitável; a confirmação chega por
      webhook verificado.
    - Recusas retornam 201 com `status=rejected` (resultado de negócio).
    - Timeout/erro incerto: retorna `status=unknown` e `outcome_unknown=true`; o sistema
      reconcilia consultando o provedor — **não** reenvie com outra chave.

    **Idempotência**: `Idempotency-Key` obrigatório; repetir devolve a mesma tentativa.

    **Banco**: insere `payments`, atualiza `orders.status` e grava `state_transitions`.
    """

    async def operation() -> OperationResult:
        payment = await container.checkout.start_payment(
            principal, order_id, payload, idempotency_key or ""
        )
        return OperationResult(201, PaymentOut.from_model(payment), "payment", str(payment.id))

    return await container.idempotency.execute(
        principal=principal,
        scope="payments.create",
        key=idempotency_key,
        fingerprint_payload={"order_id": str(order_id), "body": payload.model_dump(mode="json")},
        operation=operation,
    )


@router.get(
    "/{order_id}/payment-status",
    response_model=PaymentStatusOut,
    responses=error_responses(401, 403, 404, 429),
    summary="Consultar estado do pagamento",
)
async def get_payment_status(
    order_id: uuid.UUID,
    container: ContainerDep,
    principal: PrincipalDep,
    refresh: Annotated[
        bool, Query(description="Consulta o Mercado Pago antes de responder (reconciliação).")
    ] = False,
) -> PaymentStatusOut:
    """Retorna o estado do pedido, a tentativa corrente e o histórico de tentativas.

    Com `refresh=true`, a tentativa ativa é reconciliada com o provedor (`GET /v1/orders/{id}`)
    antes da resposta. Somente o dono do pedido ou `admin`.
    """
    order, payments = await container.payments.get_payment_status(
        principal, order_id, refresh=refresh
    )
    items = [PaymentOut.from_model(p) for p in payments]
    current = next(
        (p for p in reversed(items) if p.status in ACTIVE_PAYMENT_STATUSES),
        items[-1] if items else None,
    )
    return PaymentStatusOut(
        order_id=order.id, order_status=order.status, current_payment=current, payments=items
    )


@router.post(
    "/{order_id}/capture",
    response_model=PaymentOut,
    responses=error_responses(401, 403, 404, 409, 422, 429, 503),
    summary="Capturar pagamento autorizado (admin)",
)
async def capture_payment(
    order_id: uuid.UUID,
    container: ContainerDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    """Captura o valor total reservado de um cartão (`POST /v1/orders/{id}/capture`).

    Requer papel `admin`. A autorização expira se não capturada em até 5 dias (regra do
    provedor). Captura parcial não é suportada pela Orders API.
    """

    async def operation() -> OperationResult:
        payment = await container.checkout.capture_payment(principal, order_id)
        return OperationResult(200, PaymentOut.from_model(payment), "payment", str(payment.id))

    return await container.idempotency.execute(
        principal=principal,
        scope="payments.capture",
        key=idempotency_key,
        fingerprint_payload={"order_id": str(order_id)},
        operation=operation,
    )
