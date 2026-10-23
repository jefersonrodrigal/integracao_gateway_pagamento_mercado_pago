from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.api.deps import ContainerDep, IdempotencyKey, PrincipalDep
from app.schemas.common import error_responses
from app.schemas.refunds import RefundCreate, RefundOut
from app.services.idempotency_service import OperationResult

router = APIRouter(prefix="/orders", tags=["refunds"])


@router.post(
    "/{order_id}/refunds",
    status_code=status.HTTP_201_CREATED,
    response_model=RefundOut,
    responses=error_responses(401, 403, 404, 409, 422, 429),
    summary="Solicitar reembolso (admin)",
)
async def request_refund(
    order_id: uuid.UUID,
    payload: RefundCreate,
    container: ContainerDep,
    principal: PrincipalDep,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    """Solicita reembolso total ou parcial do pagamento aprovado do pedido.

    **Regras de negócio**
    - Requer papel `admin` (operação do lojista).
    - `amount` omitido → reembolsa o saldo restante. O valor não pode exceder o pago menos
      reembolsos aprovados/pendentes (422 `invalid_refund_amount`).
    - Um reembolso em andamento por pagamento (409 `refund_in_progress`).
    - Chama `POST /v1/orders/{id}/refund` com `X-Idempotency-Key` próprio. Total: sem corpo;
      parcial: `transactions: [{id, amount}]`.
    - Recusa do provedor → 409 com o código do provedor (ex.: `refund_amount_exceeds`).
    - Resultado incerto → 201 com `status=pending`; o worker reconcilia.

    **Banco**: insere `refunds`, atualiza `payments.refunded_amount/status` e
    `orders.status` (`partially_refunded`/`refunded`), com auditoria.
    """

    async def operation() -> OperationResult:
        refund = await container.refunds.request_refund(
            principal, order_id, payload, idempotency_key or ""
        )
        return OperationResult(201, RefundOut.model_validate(refund), "refund", str(refund.id))

    return await container.idempotency.execute(
        principal=principal,
        scope="refunds.create",
        key=idempotency_key,
        fingerprint_payload={"order_id": str(order_id), "body": payload.model_dump(mode="json")},
        operation=operation,
    )
