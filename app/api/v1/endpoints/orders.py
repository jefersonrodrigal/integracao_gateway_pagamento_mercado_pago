from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.api.deps import ContainerDep, IdempotencyKey, PrincipalDep
from app.schemas.common import error_responses
from app.schemas.orders import OrderCreate, OrderOut
from app.services.idempotency_service import OperationResult

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=OrderOut,
    responses=error_responses(401, 403, 409, 422, 429),
    summary="Criar pedido",
)
async def create_order(
    payload: OrderCreate,
    container: ContainerDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    """Cria um pedido para o cliente autenticado.

    **Regras de negócio**
    - O cliente envia apenas SKUs e quantidades; preços, desconto (cupom) e frete são
      calculados no servidor a partir do catálogo, com `Decimal` e arredondamento HALF_UP.
    - Produtos inexistentes/inativos → 422 `product_unavailable`; estoque insuficiente →
      409 `insufficient_stock`. O estoque é reservado (decrementado) na criação.
    - O pedido nasce em `pending_payment`.

    **Idempotência**: `Idempotency-Key` obrigatório; repetir a mesma requisição devolve o
    mesmo pedido (cabeçalho `Idempotent-Replayed: true`).

    **Banco**: insere `orders`, `order_items`, atualiza `products.stock`/`coupons.used_count`
    e registra a transição em `state_transitions`, tudo em uma única transação.
    """

    async def operation() -> OperationResult:
        order = await container.orders.create_order(principal, payload)
        return OperationResult(201, OrderOut.model_validate(order), "order", str(order.id))

    return await container.idempotency.execute(
        principal=principal,
        scope="orders.create",
        key=idempotency_key,
        fingerprint_payload=payload.model_dump(mode="json"),
        operation=operation,
    )


@router.get(
    "/{order_id}",
    response_model=OrderOut,
    responses=error_responses(401, 403, 404, 429),
    summary="Consultar pedido",
)
async def get_order(
    order_id: uuid.UUID, container: ContainerDep, principal: PrincipalDep
) -> OrderOut:
    """Retorna o pedido se pertencer ao cliente autenticado (ou para `admin`).

    Pedidos de outros clientes retornam 404 para não revelar sua existência (proteção IDOR).
    Sem efeitos no banco.
    """
    return OrderOut.model_validate(await container.orders.get_order(principal, order_id))


@router.post(
    "/{order_id}/cancel",
    response_model=OrderOut,
    responses=error_responses(401, 403, 404, 409, 422, 429, 502, 503),
    summary="Solicitar cancelamento",
)
async def cancel_order(
    order_id: uuid.UUID,
    container: ContainerDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    """Cancela um pedido ainda não pago (dono do pedido ou `admin`).

    **Regras de negócio**
    - `pending_payment` sem tentativa ativa: cancelado localmente.
    - Tentativa ativa (Pix/boleto pendente ou cartão autorizado): chama
      `POST /v1/orders/{id}/cancel` no Mercado Pago (permitido apenas para orders em
      `created`/`action_required`) e confirma o estado consultando o provedor.
    - Resultado do pagamento ainda desconhecido → 409 `payment_outcome_pending`.
    - Pedido já pago → 409 `order_not_cancellable` (use reembolso).
    - Pedido já cancelado → 200 com o estado atual (operação idempotente).

    **Banco**: pedido → `cancelled`, estoque e uso de cupom devolvidos, auditoria registrada.
    """

    async def operation() -> OperationResult:
        order = await container.checkout.cancel_order(principal, order_id)
        return OperationResult(200, OrderOut.model_validate(order), "order", str(order.id))

    return await container.idempotency.execute(
        principal=principal,
        scope="orders.cancel",
        key=idempotency_key,
        fingerprint_payload={"order_id": str(order_id)},
        operation=operation,
    )
