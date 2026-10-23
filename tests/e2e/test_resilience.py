"""Timeouts, concorrência, falhas de persistência e reconciliação."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.container import Container
from app.models.payment import Payment
from app.models.product import Product
from tests.conftest import PIX, card, idem
from tests.fakes.mercadopago import FakeMercadoPago
from tests.helpers import create_order, pay, payment_status

pytestmark = pytest.mark.e2e


async def _backdate_payments(container: Container, seconds: int) -> None:
    past = datetime.now(UTC) - timedelta(seconds=seconds)
    async with container.sessionmaker() as session, session.begin():
        await session.execute(
            update(Payment).values(created_at=past, updated_at=past, last_reconciled_at=None)
        )


async def test_timeout_on_create_is_reconciled_without_duplicate_charge(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    fake_mp.create_failures = ["timeout_after_commit"]
    response = await pay(api, customer, order["id"], card())
    assert response.status_code == 201, response.text
    payment = response.json()
    # O retry com a mesma chave recebeu 409 (já usada) e a busca por external_reference
    # encontrou a cobrança original: estado real aplicado, nenhuma nova cobrança criada.
    assert payment["status"] == "approved"
    assert len(fake_mp.orders) == 1
    keys = {r.headers["x-idempotency-key"] for r in fake_mp.calls("POST", "/v1/orders")}
    assert keys == {f"create-{payment['id']}"}
    assert fake_mp.calls("GET", "/v1/orders")  # busca de reconciliação
    assert (await payment_status(api, customer, order["id"]))["order_status"] == "paid"


async def test_unknown_outcome_blocks_new_attempts_until_reconciled(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    fake_mp.create_failures = ["http_500"] * 3  # todas as tentativas: resultado incerto
    payment = (await pay(api, customer, order["id"], PIX)).json()
    assert payment["status"] == "unknown" and payment["outcome_unknown"] is True

    blocked = await pay(api, customer, order["id"], PIX)
    assert blocked.json()["error"]["code"] == "payment_in_progress"
    cancel = await api.post(f"/api/v1/orders/{order['id']}/cancel", headers={**customer, **idem()})
    assert cancel.json()["error"]["code"] == "payment_outcome_pending"

    # Nada foi criado no provedor; após a janela de busca o worker conclui como falha.
    await _backdate_payments(container, container.settings.unknown_payment_give_up_seconds + 60)
    assert await container.reconciliation.reconcile_payments(10) == 1
    status = await payment_status(api, customer, order["id"])
    assert status["current_payment"]["status"] == "failed"
    assert status["order_status"] == "pending_payment"
    retry = await pay(api, customer, order["id"], PIX)
    assert retry.json()["status"] == "pending"


async def test_definitive_provider_rejection_of_request(
    api: httpx.AsyncClient,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    fake_mp.create_failures = ["invalid_properties"]
    response = await pay(api, customer, order["id"], PIX)
    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    fake_mp.create_failures = ["http_401"]
    response = await pay(api, customer, order["id"], PIX)
    assert response.status_code == 502  # credenciais da loja inválidas: erro do provedor
    assert (await payment_status(api, customer, order["id"]))["order_status"] == "pending_payment"


async def test_provider_unavailable_before_sending(
    api: httpx.AsyncClient,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    fake_mp.create_failures = ["connect_error"] * 3
    response = await pay(api, customer, order["id"], PIX)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "payment_provider_unavailable"
    assert (await pay(api, customer, order["id"], PIX)).json()["status"] == "pending"


async def test_concurrent_payment_attempts_create_single_charge(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    responses = await asyncio.gather(
        *(pay(api, customer, order["id"], card(f"approved-{i}")) for i in range(8))
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(201) == 1
    assert all(code == 409 for code in statuses if code != 201)
    assert len(fake_mp.orders) == 1
    async with container.sessionmaker() as session:
        assert len((await session.execute(select(Payment))).scalars().all()) == 1


async def test_concurrent_orders_never_oversell_stock(
    api: httpx.AsyncClient,
    container: Container,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    async def buy() -> int:
        response = await api.post(
            "/api/v1/orders",
            json={"items": [{"sku": "SKU-B", "quantity": 1}]},
            headers={**customer, **idem()},
        )
        return response.status_code

    results = await asyncio.gather(*(buy() for _ in range(6)))
    assert sorted(results) == [201, 201, 201, 409, 409, 409]
    async with container.sessionmaker() as session:
        product = (
            await session.execute(select(Product).where(Product.sku == "SKU-B"))
        ).scalar_one()
    assert product.stock == 0


async def test_persistence_failure_after_charge_is_recovered_by_reconciliation(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = await create_order(api, customer)
    original = container.payments.apply_provider_order
    calls = {"n": 0}

    async def flaky(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("database connection lost")
        return await original(*args, **kwargs)

    monkeypatch.setattr(container.payments, "apply_provider_order", flaky)
    from app.main import create_app

    transport = httpx.ASGITransport(
        app=create_app(container.settings, container), raise_app_exceptions=False
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as raw:
        response = await pay(raw, customer, order["id"], card())
    assert response.status_code == 500
    assert "database connection lost" not in response.text  # erro interno não vaza detalhes
    assert len(fake_mp.orders) == 1  # cobrança efetivada no provedor

    status = await payment_status(api, customer, order["id"])
    assert status["current_payment"]["status"] == "created"
    # Repetir com outra chave não cria nova cobrança: a tentativa ativa bloqueia.
    assert (await pay(api, customer, order["id"], card())).status_code == 409

    await _backdate_payments(container, container.settings.reconcile_interval_seconds + 60)
    assert await container.reconciliation.reconcile_payments(10) == 1
    status = await payment_status(api, customer, order["id"])
    assert status["current_payment"]["status"] == "approved"
    assert status["order_status"] == "paid"
    assert len(fake_mp.orders) == 1


async def test_status_refresh_reconciles_with_provider(
    api: httpx.AsyncClient,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    payment = (await pay(api, customer, order["id"], PIX)).json()
    fake_mp.approve(payment["provider_order_id"])  # webhook perdido
    assert (await payment_status(api, customer, order["id"]))["order_status"] == "processing"
    assert (await payment_status(api, customer, order["id"], refresh=True))[
        "order_status"
    ] == "paid"


async def test_rate_limiting(settings, container: Container) -> None:  # type: ignore[no-untyped-def]
    from app.main import create_app

    app = create_app(settings.model_copy(update={"rate_limit_per_minute": 3}), container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as api:
        codes = [(await api.get("/api/v1/payment-methods")).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
