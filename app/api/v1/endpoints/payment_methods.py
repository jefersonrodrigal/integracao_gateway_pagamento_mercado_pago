from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ContainerDep, PrincipalDep
from app.schemas.common import error_responses
from app.schemas.payment_methods import PaymentMethodsOut

router = APIRouter(prefix="/payment-methods", tags=["payment-methods"])


@router.get(
    "",
    response_model=PaymentMethodsOut,
    responses=error_responses(401, 429, 503),
    summary="Consultar meios de pagamento disponíveis",
)
async def list_payment_methods(container: ContainerDep, _: PrincipalDep) -> PaymentMethodsOut:
    """Meios habilitados na conta do Mercado Pago (`GET /v1/payment_methods`) e suportados
    por esta integração: Pix, boleto, cartões de crédito e débito (com bandeiras).

    Resultado em cache (`MERCADO_PAGO_PAYMENT_METHODS_CACHE_SECONDS`); em falha do provedor
    serve o último catálogo conhecido. Sem efeitos no banco.
    """
    return await container.payment_methods.list_methods()
