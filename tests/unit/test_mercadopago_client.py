"""Cliente HTTP: contrato das requisições, retries e classificação de resultado."""

import json
from decimal import Decimal

import httpx
import pytest

from app.domain.states import PaymentMethod
from app.integrations.mercadopago.client import MercadoPagoClient
from app.integrations.mercadopago.exceptions import (
    MercadoPagoAPIError,
    MercadoPagoOutcomeUnknownError,
    MercadoPagoUnavailableError,
)
from app.integrations.mercadopago.orders import OrderPaymentInput, build_create_order_payload
from tests.conftest import make_settings
from tests.fakes.mercadopago import FakeMercadoPago

PAYLOAD = build_create_order_payload(
    OrderPaymentInput(
        external_reference="pay_abc",
        amount=Decimal("50"),
        method=PaymentMethod.PIX,
        payment_method_id="pix",
        payer={"email": "a@testuser.com"},
        expiration_time="PT1H",
    )
)


async def _no_sleep(_: float) -> None:
    return None


def _client(handler: httpx.MockTransport | FakeMercadoPago, retries: int = 2) -> MercadoPagoClient:
    transport = handler.transport() if isinstance(handler, FakeMercadoPago) else handler
    return MercadoPagoClient(
        make_settings(mercado_pago_max_retries=retries), transport=transport, sleep=_no_sleep
    )


def test_pix_payload_matches_official_contract() -> None:
    assert PAYLOAD == {
        "type": "online",
        "processing_mode": "automatic",
        "external_reference": "pay_abc",
        "total_amount": "50.00",
        "payer": {"email": "a@testuser.com"},
        "transactions": {
            "payments": [
                {
                    "amount": "50.00",
                    "payment_method": {"id": "pix", "type": "bank_transfer"},
                    "expiration_time": "PT1H",
                }
            ]
        },
    }


def test_card_payload_with_manual_capture() -> None:
    payload = build_create_order_payload(
        OrderPaymentInput(
            external_reference="pay_x",
            amount=Decimal("10.5"),
            method=PaymentMethod.CREDIT_CARD,
            payment_method_id="visa",
            payer={"email": "a@testuser.com"},
            card_token="tok",
            installments=3,
            capture_manual=True,
            statement_descriptor="LOJA",
        )
    )
    method = payload["transactions"]["payments"][0]["payment_method"]
    assert payload["capture_mode"] == "manual"
    assert method == {
        "id": "visa",
        "type": "credit_card",
        "token": "tok",
        "installments": 3,
        "statement_descriptor": "LOJA",
    }


async def test_create_sends_auth_and_idempotency_headers() -> None:
    fake = FakeMercadoPago()
    client = _client(fake)
    order = await client.create_order(PAYLOAD, "create-key-1")
    request = fake.calls("POST", "/v1/orders")[0]
    assert request.headers["x-idempotency-key"] == "create-key-1"
    assert request.headers["authorization"].startswith("Bearer APP_USR-")
    assert order.status == "action_required"
    assert order.payment and order.payment.payment_method.qr_code
    await client.aclose()


async def test_write_retries_reuse_same_key_and_report_unknown_after_server_errors() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-idempotency-key"])
        return httpx.Response(500, json={"errors": [{"code": "internal_error"}]})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(MercadoPagoOutcomeUnknownError):
        await client.create_order(PAYLOAD, "same-key")
    assert seen == ["same-key"] * 3
    await client.aclose()


async def test_timeout_then_already_used_key_is_unknown_not_new_charge() -> None:
    fake = FakeMercadoPago()
    fake.create_failures = ["timeout_after_commit"]
    client = _client(fake)
    with pytest.raises(MercadoPagoOutcomeUnknownError) as exc:
        await client.create_order(PAYLOAD, "key-timeout")
    assert exc.value.reason == "idempotency_key_already_used"
    assert len(fake.orders) == 1  # nenhuma cobrança duplicada
    await client.aclose()


async def test_connect_errors_only_mean_definitely_not_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(httpx.MockTransport(handler), retries=1)
    with pytest.raises(MercadoPagoUnavailableError):
        await client.create_order(PAYLOAD, "key-connect")
    await client.aclose()


async def test_rate_limit_is_retried_honoring_retry_after() -> None:
    delays: list[float] = []
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"id": "ORD1"}),
        ]
    )

    async def record(delay: float) -> None:
        delays.append(delay)

    client = MercadoPagoClient(
        make_settings(), transport=httpx.MockTransport(lambda r: next(responses)), sleep=record
    )
    order = await client.get_order("ORD1")
    assert order.id == "ORD1"
    assert delays == [2.0]
    await client.aclose()


async def test_definitive_errors_are_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400, json={"errors": [{"code": "invalid_properties", "message": "bad"}]}
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(MercadoPagoAPIError) as exc:
        await client.create_order(PAYLOAD, "k-400")
    assert (calls, exc.value.status_code, exc.value.code) == (1, 400, "invalid_properties")
    await client.aclose()


async def test_partial_refund_body_and_search_params() -> None:
    bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert {"begin_date", "end_date", "external_reference"} <= set(request.url.params)
            return httpx.Response(200, json={"data": []})
        bodies.append(json.loads(request.content) if request.content else None)
        return httpx.Response(201, json={"id": "ORD1", "status": "processed"})

    client = _client(httpx.MockTransport(handler))
    await client.refund_order("ORD1", "r1", [{"id": "PAY1", "amount": "5.00"}])
    await client.refund_order("ORD1", "r2")
    from datetime import UTC, datetime

    assert (
        await client.search_orders_by_reference("pay_x", datetime.now(UTC), datetime.now(UTC)) == []
    )
    assert bodies == [{"transactions": [{"id": "PAY1", "amount": "5.00"}]}, None]
    await client.aclose()


async def test_rejects_unsafe_external_ids() -> None:
    client = _client(FakeMercadoPago())
    with pytest.raises(ValueError, match="inválido"):
        await client.get_order("../v1/payments")
    await client.aclose()
