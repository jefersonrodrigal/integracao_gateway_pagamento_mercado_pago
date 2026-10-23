import asyncio
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.container import Container
from app.models.product import Product
from app.models.refund import Refund
from tests.conftest import PIX, card, idem
from tests.fakes.mercadopago import FakeMercadoPago
from tests.helpers import create_order, pay, payment_status

pytestmark = pytest.mark.e2e


async def _refund(
    api: httpx.AsyncClient,
    headers: dict[str, str],
    order_id: str,
    body: dict[str, object] | None = None,
) -> httpx.Response:
    return await api.post(
        f"/api/v1/orders/{order_id}/refunds", json=body or {}, headers={**headers, **idem()}
    )


async def _paid_order(api: httpx.AsyncClient, customer: dict[str, str]) -> dict[str, object]:
    order = await create_order(api, customer)  # total 119.80
    assert (await pay(api, customer, order["id"], card())).json()["status"] == "approved"
    return order


async def test_cancel_pending_pix_at_provider(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    payment = (await pay(api, customer, order["id"], PIX)).json()
    response = await api.post(
        f"/api/v1/orders/{order['id']}/cancel", headers={**customer, **idem()}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    cancel_call = fake_mp.calls("POST", f"/v1/orders/{payment['provider_order_id']}/cancel")[0]
    assert cancel_call.headers["x-idempotency-key"] == f"cancel-{payment['id']}"
    status = await payment_status(api, customer, order["id"])
    assert status["current_payment"]["status"] == "cancelled"
    async with container.sessionmaker() as session:
        product = (
            await session.execute(select(Product).where(Product.sku == "SKU-A"))
        ).scalar_one()
    assert product.stock == 10  # estoque devolvido


async def test_cancel_paid_order_is_refused(
    api: httpx.AsyncClient, customer: dict[str, str], catalog: dict[str, Product]
) -> None:
    order = await _paid_order(api, customer)
    response = await api.post(
        f"/api/v1/orders/{order['id']}/cancel", headers={**customer, **idem()}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "order_not_cancellable"


async def test_cancel_race_with_approval_keeps_paid_state(
    api: httpx.AsyncClient,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    payment = (await pay(api, customer, order["id"], PIX)).json()
    fake_mp.approve(payment["provider_order_id"])  # pago antes do cancelamento chegar
    response = await api.post(
        f"/api/v1/orders/{order['id']}/cancel", headers={**customer, **idem()}
    )
    assert response.status_code == 409
    status = await payment_status(api, customer, order["id"])
    assert status["order_status"] == "paid"


async def test_partial_then_full_refund(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    admin: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await _paid_order(api, customer)
    assert (await _refund(api, customer, order["id"], {"amount": "10.00"})).status_code == 403

    partial = await _refund(api, admin, order["id"], {"amount": "19.80", "reason": "avaria"})
    assert partial.status_code == 201, partial.text
    assert partial.json()["status"] == "approved" and partial.json()["is_full"] is False
    refund_call = fake_mp.calls("POST", "/v1/orders/")[-1]
    assert b'"amount":"19.80"' in refund_call.content.replace(b" ", b"")
    status = await payment_status(api, customer, order["id"])
    assert status["order_status"] == "partially_refunded"
    assert status["current_payment"]["refunded_amount"] == "19.80"

    excessive = await _refund(api, admin, order["id"], {"amount": "100.01"})
    assert excessive.status_code == 422
    assert excessive.json()["error"]["details"]["refundable"] == "100.00"

    rest = await _refund(api, admin, order["id"])
    assert rest.status_code == 201
    assert rest.json()["amount"] == "100.00"
    status = await payment_status(api, customer, order["id"])
    assert status["order_status"] == "refunded"
    assert status["current_payment"]["status"] == "refunded"
    assert (await _refund(api, admin, order["id"])).status_code == 409


async def test_full_refund_sends_empty_body(
    api: httpx.AsyncClient,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    admin: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await _paid_order(api, customer)
    response = await _refund(api, admin, order["id"])
    assert response.json()["is_full"] is True and response.json()["amount"] == "119.80"
    assert fake_mp.calls("POST", "/v1/orders/")[-1].content == b""
    assert (await payment_status(api, customer, order["id"]))["order_status"] == "refunded"


async def test_refund_of_unpaid_order_is_refused(
    api: httpx.AsyncClient,
    customer: dict[str, str],
    admin: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    response = await _refund(api, admin, order["id"])
    assert response.status_code == 409 and response.json()["error"]["code"] == "not_refundable"


async def test_concurrent_refunds_never_exceed_paid_amount(
    api: httpx.AsyncClient,
    container: Container,
    customer: dict[str, str],
    admin: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await _paid_order(api, customer)
    responses = await asyncio.gather(
        *(_refund(api, admin, order["id"], {"amount": "60.00"}) for _ in range(4))
    )
    async with container.sessionmaker() as session:
        refunds = (await session.execute(select(Refund))).scalars().all()
    total = sum(r.amount for r in refunds if r.status in ("approved", "pending"))
    assert total == Decimal("60.00")  # 2 x 60,00 excederia 119,80
    assert sorted(r.status_code for r in responses)[0] == 201
    assert sum(1 for r in responses if r.status_code == 201) == 1


async def test_refund_timeout_is_reconciled(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    admin: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await _paid_order(api, customer)
    fake_mp.refund_failures = ["read_timeout", "read_timeout", "read_timeout"]
    response = await _refund(api, admin, order["id"], {"amount": "10.00"})
    assert response.status_code == 201
    assert response.json()["status"] == "pending"

    async with container.sessionmaker() as session, session.begin():
        refund = (await session.execute(select(Refund))).scalar_one()
        refund.updated_at = refund.created_at.replace(year=2000)
    assert await container.reconciliation.reconcile_refunds(10) == 1
    async with container.sessionmaker() as session:
        refund = (await session.execute(select(Refund))).scalar_one()
    assert refund.status == "approved" and refund.provider_refund_id
    assert (await payment_status(api, customer, order["id"]))[
        "order_status"
    ] == "partially_refunded"
