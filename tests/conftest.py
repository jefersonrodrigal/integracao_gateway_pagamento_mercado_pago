from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import command
from app.container import Container
from app.core.config import Settings, load_settings
from app.core.security import create_access_token
from app.integrations.mercadopago.client import MercadoPagoClient
from app.integrations.mercadopago.webhooks import build_manifest, compute_signature
from app.main import create_app
from app.models.product import Coupon, CouponKind, Product
from tests.fakes.mercadopago import FakeMercadoPago

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres@localhost:55432/payments_test"
)
WEBHOOK_SECRET = "test-webhook-secret"
TABLES = (
    "state_transitions, payment_events, refunds, payments, order_items, orders, "
    "idempotency_records, coupons, products"
)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": TEST_DATABASE_URL,
        "mercado_pago_access_token": "APP_USR-0000000000-test-token",
        "mercado_pago_webhook_secret": WEBHOOK_SECRET,
        "mercado_pago_base_url": "https://api.mercadopago.test",
        "jwt_secret": "test-jwt-secret-with-at-least-32-characters",
        "log_json": False,
        "log_level": "WARNING",
        "rate_limit_per_minute": 10_000,
        "webhook_process_inline": False,
        "_env_file": None,
    }
    values.update(overrides)
    return load_settings(**values)


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["database_url"] = url
    return config


@pytest.fixture(scope="session")
def settings() -> Settings:
    return make_settings()


@pytest.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    db_engine = create_async_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=10)
    try:
        async with db_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"PostgreSQL de testes indisponível ({TEST_DATABASE_URL}): {exc}")
    # Migrations reais (as mesmas de produção), executadas fora do event loop do teste.
    await asyncio.to_thread(command.downgrade, alembic_config(TEST_DATABASE_URL), "base")
    await asyncio.to_thread(command.upgrade, alembic_config(TEST_DATABASE_URL), "head")
    yield db_engine
    await db_engine.dispose()


@pytest.fixture
async def clean_db(engine: AsyncEngine) -> AsyncEngine:
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    return engine


@pytest.fixture
def fake_mp() -> FakeMercadoPago:
    return FakeMercadoPago()


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def container(
    settings: Settings, clean_db: AsyncEngine, fake_mp: FakeMercadoPago
) -> AsyncIterator[Container]:
    client = MercadoPagoClient(settings, transport=fake_mp.transport(), sleep=_no_sleep)
    built = Container.build(settings, engine=clean_db, mp_client=client)
    yield built
    await client.aclose()


@pytest.fixture
async def api(settings: Settings, container: Container) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def catalog(container: Container) -> dict[str, Product]:
    products = {
        "SKU-A": Product(sku="SKU-A", name="Produto A", unit_price=Decimal("59.90"), stock=10),
        "SKU-B": Product(sku="SKU-B", name="Produto B", unit_price=Decimal("10.05"), stock=3),
        "SKU-OFF": Product(
            sku="SKU-OFF", name="Inativo", unit_price=Decimal("5.00"), stock=5, active=False
        ),
    }
    async with container.sessionmaker() as session, session.begin():
        session.add_all(products.values())
        session.add(Coupon(code="DEZ", kind=CouponKind.PERCENT, value=Decimal("10.00")))
        session.add(Coupon(code="UMUSO", kind=CouponKind.FIXED, value=Decimal("5.00"), max_uses=1))
    return products


def auth(settings: Settings, subject: str = "customer-1", *roles: str) -> dict[str, str]:
    token = create_access_token(settings, subject, list(roles) or ["customer"])
    return {"Authorization": f"Bearer {token}"}


def idem() -> dict[str, str]:
    return {"Idempotency-Key": f"test-{uuid.uuid4()}"}


@pytest.fixture
def customer(settings: Settings) -> dict[str, str]:
    return auth(settings, "customer-1")


@pytest.fixture
def admin(settings: Settings) -> dict[str, str]:
    return auth(settings, "admin-1", "admin")


def signed_webhook(
    order_id: str,
    *,
    secret: str = WEBHOOK_SECRET,
    action: str = "order.processed",
    ts: int | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, str], bytes]:
    """Monta (params, headers, body) de uma notificação assinada como o Mercado Pago."""
    import json

    request_id = str(uuid.uuid4())
    ts_value = str(ts if ts is not None else int(time.time()))
    signature = compute_signature(secret, build_manifest(order_id, request_id, ts_value))
    body = {
        "action": action,
        "api_version": "v1",
        "type": "order",
        "live_mode": False,
        "data": {"id": order_id, **(extra or {})},
    }
    headers = {
        "x-signature": f"ts={ts_value},v1={signature}",
        "x-request-id": request_id,
        "content-type": "application/json",
    }
    return {"data.id": order_id, "type": "order"}, headers, json.dumps(body).encode()


PIX = {"method": "pix", "payer": {"email": "comprador@testuser.com"}}
VALID_CPF = "19119119100"
BOLETO = {
    "method": "boleto",
    "payer": {
        "email": "comprador@testuser.com",
        "first_name": "APRO",
        "last_name": "Teste",
        "identification": {"type": "CPF", "number": VALID_CPF},
        "address": {
            "zip_code": "06233-903",
            "street_name": "Av. das Nações Unidas",
            "street_number": "3003",
            "neighborhood": "Bonfim",
            "city": "Osasco",
            "state": "SP",
        },
    },
}


def card(token: str = "approved-token-123", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "method": "credit_card",
        "token": token,
        "payment_method_id": "master",
        "installments": 1,
        "payer": {"email": "comprador@testuser.com"},
    }
    payload.update(overrides)
    return payload
