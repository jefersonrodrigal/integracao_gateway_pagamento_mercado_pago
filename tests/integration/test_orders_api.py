import uuid

import httpx
import pytest
from sqlalchemy import select

from app.container import Container
from app.models.audit import StateTransition
from app.models.product import Coupon, Product
from tests.conftest import auth, idem
from tests.helpers import create_order

pytestmark = pytest.mark.integration


async def test_create_order_calculates_on_server_and_reserves_stock(
    api: httpx.AsyncClient,
    container: Container,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(
        api,
        customer,
        [{"sku": "SKU-A", "quantity": 2}, {"sku": "SKU-B", "quantity": 3}],
        coupon_code="DEZ",
        reference="pedido-loja-42",
    )
    assert order["status"] == "pending_payment"
    assert order["subtotal"] == "149.95"
    assert order["discount_total"] == "15.00"  # 10% de 149.95 = 14.995 → 15.00 (HALF_UP)
    assert order["total_amount"] == "134.95"
    assert order["customer_id"] == "customer-1"
    assert {item["sku"]: item["line_total"] for item in order["items"]} == {
        "SKU-A": "119.80",
        "SKU-B": "30.15",
    }
    async with container.sessionmaker() as session:
        stock = {p.sku: p.stock for p in (await session.execute(select(Product))).scalars()}
        coupon = (await session.execute(select(Coupon).where(Coupon.code == "DEZ"))).scalar_one()
        audit = (await session.execute(select(StateTransition))).scalars().all()
    assert stock["SKU-A"] == 8 and stock["SKU-B"] == 0
    assert coupon.used_count == 1
    assert [a.to_status for a in audit] == ["pending_payment"]


@pytest.mark.parametrize(
    ("items", "status", "code"),
    [
        ([{"sku": "SKU-B", "quantity": 4}], 409, "insufficient_stock"),
        ([{"sku": "SKU-OFF", "quantity": 1}], 422, "product_unavailable"),
        ([{"sku": "NOPE", "quantity": 1}], 422, "product_unavailable"),
    ],
)
async def test_create_order_business_errors(
    api: httpx.AsyncClient,
    customer: dict[str, str],
    catalog: dict[str, Product],
    items: list[dict[str, object]],
    status: int,
    code: str,
) -> None:
    response = await api.post(
        "/api/v1/orders", json={"items": items}, headers={**customer, **idem()}
    )
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["request_id"]


async def test_client_supplied_prices_are_rejected(
    api: httpx.AsyncClient, customer: dict[str, str], catalog: dict[str, Product]
) -> None:
    body = {"items": [{"sku": "SKU-A", "quantity": 1}], "total_amount": "0.01"}
    response = await api.post("/api/v1/orders", json=body, headers={**customer, **idem()})
    assert response.status_code == 422
    assert "0.01" not in response.text  # erros não ecoam valores de entrada


async def test_coupon_max_uses_enforced(
    api: httpx.AsyncClient, customer: dict[str, str], catalog: dict[str, Product]
) -> None:
    await create_order(api, customer, coupon_code="UMUSO")
    response = await api.post(
        "/api/v1/orders",
        json={"items": [{"sku": "SKU-A", "quantity": 1}], "coupon_code": "UMUSO"},
        headers={**customer, **idem()},
    )
    assert response.json()["error"]["code"] == "invalid_coupon"


async def test_authentication_and_idor_protection(
    api: httpx.AsyncClient,
    settings,
    customer: dict[str, str],
    admin: dict[str, str],  # type: ignore[no-untyped-def]
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer)
    url = f"/api/v1/orders/{order['id']}"
    assert (await api.get(url)).status_code == 401
    assert (await api.get(url, headers={"Authorization": "Bearer invalid"})).status_code == 401
    other = auth(settings, "customer-2")
    response = await api.get(url, headers=other)
    assert response.status_code == 404  # não revela que o pedido existe
    assert (await api.get(url, headers=customer)).status_code == 200
    assert (await api.get(url, headers=admin)).status_code == 200
    assert (await api.get(f"/api/v1/orders/{uuid.uuid4()}", headers=customer)).status_code == 404


async def test_idempotent_order_creation(
    api: httpx.AsyncClient,
    container: Container,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    key = idem()
    body = {"items": [{"sku": "SKU-A", "quantity": 1}]}
    first = await api.post("/api/v1/orders", json=body, headers={**customer, **key})
    second = await api.post("/api/v1/orders", json=body, headers={**customer, **key})
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.headers["idempotent-replayed"] == "true"

    changed = await api.post(
        "/api/v1/orders",
        json={"items": [{"sku": "SKU-A", "quantity": 2}]},
        headers={**customer, **key},
    )
    assert changed.status_code == 422
    assert changed.json()["error"]["code"] == "idempotency_key_reused"

    missing = await api.post("/api/v1/orders", json=body, headers=customer)
    assert missing.json()["error"]["code"] == "invalid_idempotency_key"
    async with container.sessionmaker() as session:
        product = (
            await session.execute(select(Product).where(Product.sku == "SKU-A"))
        ).scalar_one()
    assert product.stock == 9  # apenas uma reserva


async def test_same_key_is_scoped_per_principal(
    api: httpx.AsyncClient,
    settings,
    customer: dict[str, str],
    catalog: dict[str, Product],  # type: ignore[no-untyped-def]
) -> None:
    key = idem()
    body = {"items": [{"sku": "SKU-A", "quantity": 1}]}
    first = await api.post("/api/v1/orders", json=body, headers={**customer, **key})
    other = await api.post(
        "/api/v1/orders", json=body, headers={**auth(settings, "customer-2"), **key}
    )
    assert first.json()["id"] != other.json()["id"]


async def test_cancel_unpaid_order_releases_stock(
    api: httpx.AsyncClient,
    container: Container,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order = await create_order(api, customer, [{"sku": "SKU-B", "quantity": 3}], coupon_code="DEZ")
    response = await api.post(
        f"/api/v1/orders/{order['id']}/cancel", headers={**customer, **idem()}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    again = await api.post(f"/api/v1/orders/{order['id']}/cancel", headers={**customer, **idem()})
    assert again.status_code == 200
    async with container.sessionmaker() as session:
        product = (
            await session.execute(select(Product).where(Product.sku == "SKU-B"))
        ).scalar_one()
        coupon = (await session.execute(select(Coupon).where(Coupon.code == "DEZ"))).scalar_one()
    assert product.stock == 3
    assert coupon.used_count == 0


async def test_health_and_metrics(api: httpx.AsyncClient) -> None:
    assert (await api.get("/health/live")).json() == {"status": "ok"}
    ready = await api.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["checks"]["database"] == "ok"
    metrics = await api.get("/metrics")
    assert "http_requests_total" in metrics.text
    response = await api.get("/health/live", headers={"X-Request-ID": "corr-12345678"})
    assert response.headers["x-request-id"] == "corr-12345678"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_payment_methods(api: httpx.AsyncClient, customer: dict[str, str]) -> None:
    response = await api.get("/api/v1/payment-methods", headers=customer)
    assert response.status_code == 200
    methods = {m["method"]: m for m in response.json()["methods"]}
    assert methods["pix"]["available"] and methods["boleto"]["available"]
    assert [b["payment_method_id"] for b in methods["credit_card"]["brands"]] == ["master", "visa"]
    assert methods["debit_card"]["available"] is False  # bandeira inativa na conta


async def test_request_body_limit(api: httpx.AsyncClient, customer: dict[str, str]) -> None:
    response = await api.post(
        "/api/v1/orders",
        content=b"x" * 2_000_000,
        headers={**customer, **idem(), "content-type": "application/json"},
    )
    assert response.status_code == 413
