"""Fluxos ponta a ponta: API → serviços → PostgreSQL → cliente HTTP real → MP simulado."""

import httpx
import pytest
from sqlalchemy import select

from app.container import Container
from app.models.audit import StateTransition
from app.models.payment import Payment
from app.models.product import Product
from tests.conftest import BOLETO, PIX, card, idem, signed_webhook
from tests.fakes.mercadopago import FakeMercadoPago
from tests.helpers import create_order, pay, payment_status

pytestmark = pytest.mark.e2e


async def _notify(api: httpx.AsyncClient, container: Container, provider_order_id: str) -> None:
    params, headers, body = signed_webhook(provider_order_id)
    response = await api.post(
        "/api/v1/webhooks/mercadopago", params=params, headers=headers, content=body
    )
    assert response.status_code == 200, response.text
    await container.webhooks.process_due(10)


async def test_pix_pending_then_confirmed_by_verified_webhook(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    response = await pay(api, customer, order["id"], PIX)
    assert response.status_code == 201, response.text
    payment = response.json()
    assert payment["status"] == "pending"
    assert payment["amount"] == order["total_amount"]  # valor do servidor
    assert payment["pix"]["qr_code"].startswith("000201")
    assert payment["provider_status_detail"] == "waiting_transfer"

    sent = fake_mp.calls("POST", "/v1/orders")[0]
    assert sent.headers["x-idempotency-key"] == f"create-{payment['id']}"
    status = await payment_status(api, customer, order["id"])
    assert status["order_status"] == "processing"

    fake_mp.approve(payment["provider_order_id"])
    await _notify(api, container, payment["provider_order_id"])

    status = await payment_status(api, customer, order["id"])
    assert status["order_status"] == "paid"
    assert status["current_payment"]["status"] == "approved"
    assert status["current_payment"]["paid_amount"] == order["total_amount"]
    async with container.sessionmaker() as session:
        transitions = (
            (await session.execute(select(StateTransition).order_by(StateTransition.id)))
            .scalars()
            .all()
        )
    paid = [t for t in transitions if t.entity_type == "order" and t.to_status == "paid"]
    assert len(paid) == 1 and paid[0].source == "webhook"


async def test_card_approved_immediately(
    api: httpx.AsyncClient,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    response = await pay(api, customer, order["id"], card(installments=3))
    assert response.status_code == 201
    assert response.json()["status"] == "approved"
    assert response.json()["installments"] == 3
    sent = fake_mp.calls("POST", "/v1/orders")[0].content.decode()
    assert '"token":"approved-token-123"' in sent.replace(" ", "")
    status = await payment_status(api, customer, order["id"])
    assert status["order_status"] == "paid"


async def test_card_rejected_allows_new_attempt(
    api: httpx.AsyncClient,
    container: Container,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    rejected = await pay(api, customer, order["id"], card("rejected-token"))
    assert rejected.status_code == 201
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["provider_status_detail"] == "rejected_by_issuer"
    assert (await payment_status(api, customer, order["id"]))["order_status"] == "pending_payment"

    approved = await pay(api, customer, order["id"], card("approved-second"))
    assert approved.json()["status"] == "approved"
    status = await payment_status(api, customer, order["id"])
    assert [p["status"] for p in status["payments"]] == ["rejected", "approved"]
    async with container.sessionmaker() as session:
        rows = (await session.execute(select(Payment))).scalars().all()
    assert all("token" not in str(r.payment_data or {}) for r in rows)  # token nunca persistido


async def test_card_in_review_then_approved(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    payment = (await pay(api, customer, order["id"], card("review-token"))).json()
    assert payment["status"] == "in_review"
    fake_mp.approve(payment["provider_order_id"])
    await _notify(api, container, payment["provider_order_id"])
    assert (await payment_status(api, customer, order["id"]))["order_status"] == "paid"


async def test_boleto_pending_then_confirmed(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    response = await pay(api, customer, order["id"], BOLETO)
    payment = response.json()
    assert payment["status"] == "pending"
    assert payment["boleto"]["digitable_line"]
    sent = fake_mp.calls("POST", "/v1/orders")[0].content.decode()
    assert '"type":"ticket"' in sent and '"expiration_time":"P3D"' in sent
    fake_mp.approve(payment["provider_order_id"])
    await _notify(api, container, payment["provider_order_id"])
    assert (await payment_status(api, customer, order["id"]))["order_status"] == "paid"


async def test_pix_expired_returns_order_to_pending_payment(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    payment = (await pay(api, customer, order["id"], PIX)).json()
    fake_mp.set_status(payment["provider_order_id"], "expired", "expired")
    await _notify(api, container, payment["provider_order_id"])
    status = await payment_status(api, customer, order["id"])
    assert status["current_payment"]["status"] == "expired"
    assert status["order_status"] == "pending_payment"


async def test_authorize_then_capture(
    api: httpx.AsyncClient,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    admin: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    payment = (await pay(api, customer, order["id"], card(capture=False))).json()
    assert payment["status"] == "authorized" and payment["capture_mode"] == "manual"
    assert (await payment_status(api, customer, order["id"]))["order_status"] == "processing"

    forbidden = await api.post(
        f"/api/v1/orders/{order['id']}/capture", headers={**customer, **idem()}
    )
    assert forbidden.status_code == 403
    captured = await api.post(f"/api/v1/orders/{order['id']}/capture", headers={**admin, **idem()})
    assert captured.status_code == 200, captured.text
    assert captured.json()["status"] == "approved"
    assert (await payment_status(api, customer, order["id"]))["order_status"] == "paid"


async def test_cannot_pay_twice_or_pay_paid_order(
    api: httpx.AsyncClient, customer: dict[str, str], catalog: dict[str, Product]
) -> None:
    order = await create_order(api, customer)
    await pay(api, customer, order["id"], PIX)
    second = await pay(api, customer, order["id"], card())
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "payment_in_progress"

    paid_order = await create_order(api, customer)
    await pay(api, customer, paid_order["id"], card())
    again = await pay(api, customer, paid_order["id"], card("approved-again"))
    assert again.json()["error"]["code"] == "order_not_payable"


async def test_idempotent_payment_request_does_not_charge_twice(
    api: httpx.AsyncClient,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    key = idem()
    first = await pay(api, customer, order["id"], card(), key)
    second = await pay(api, customer, order["id"], card(), key)
    assert first.json()["id"] == second.json()["id"]
    assert second.headers["idempotent-replayed"] == "true"
    assert len(fake_mp.calls("POST", "/v1/orders")) == 1
