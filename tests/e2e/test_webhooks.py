import time

import httpx
import pytest
from sqlalchemy import select

from app.container import Container
from app.domain.states import EventStatus
from app.models.audit import StateTransition
from app.models.payment_event import PaymentEvent
from app.models.product import Product
from tests.conftest import PIX, signed_webhook
from tests.fakes.mercadopago import FakeMercadoPago
from tests.helpers import create_order, pay, payment_status

pytestmark = pytest.mark.e2e
URL = "/api/v1/webhooks/mercadopago"


async def _pix_order(api: httpx.AsyncClient, customer: dict[str, str]) -> tuple[dict, dict]:  # type: ignore[type-arg]
    order = await create_order(api, customer)
    payment = (await pay(api, customer, order["id"], PIX)).json()
    return order, payment


async def _events(container: Container) -> list[PaymentEvent]:
    async with container.sessionmaker() as session:
        return list((await session.execute(select(PaymentEvent))).scalars())


async def test_invalid_signatures_are_rejected_and_not_persisted(
    api: httpx.AsyncClient,
    container: Container,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    _, payment = await _pix_order(api, customer)
    provider_id = payment["provider_order_id"]
    params, headers, body = signed_webhook(provider_id, secret="wrong-secret")
    assert (await api.post(URL, params=params, headers=headers, content=body)).status_code == 401

    params, headers, body = signed_webhook(provider_id)
    tampered = {**params, "data.id": "ORD01ATTACKER"}
    assert (await api.post(URL, params=tampered, headers=headers, content=body)).status_code == 401

    params, headers, body = signed_webhook(provider_id, ts=int(time.time()) - 7200)
    assert (await api.post(URL, params=params, headers=headers, content=body)).status_code == 401

    no_sig = {k: v for k, v in headers.items() if k != "x-signature"}
    assert (await api.post(URL, params=params, headers=no_sig, content=body)).status_code == 401
    assert await _events(container) == []


async def test_body_claims_are_never_trusted(
    api: httpx.AsyncClient,
    container: Container,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order, payment = await _pix_order(api, customer)
    # Assinatura válida, corpo alegando aprovação — mas o provedor diz que segue pendente.
    params, headers, body = signed_webhook(
        payment["provider_order_id"], extra={"status": "processed", "status_detail": "accredited"}
    )
    assert (await api.post(URL, params=params, headers=headers, content=body)).status_code == 200
    await container.webhooks.process_due(10)
    status = await payment_status(api, customer, order["id"])
    assert status["order_status"] == "processing"
    assert status["current_payment"]["status"] == "pending"


async def test_duplicate_notifications_are_processed_once(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order, payment = await _pix_order(api, customer)
    fake_mp.approve(payment["provider_order_id"])
    params, headers, body = signed_webhook(payment["provider_order_id"])
    first = await api.post(URL, params=params, headers=headers, content=body)
    second = await api.post(URL, params=params, headers=headers, content=body)
    assert first.json()["status"] == "received"
    assert second.json()["status"] == "duplicate"
    assert first.json()["event_id"] == second.json()["event_id"]
    await container.webhooks.process_due(10)

    # Um reenvio com novo x-request-id (corpo idêntico) também é deduplicado.
    params2, headers2, body2 = signed_webhook(payment["provider_order_id"])
    assert body2 == body
    third = await api.post(URL, params=params2, headers=headers2, content=body2)
    assert third.json()["status"] == "duplicate"

    events = await _events(container)
    assert len(events) == 1 and events[0].duplicate_count == 2
    assert events[0].status == EventStatus.PROCESSED
    assert events[0].payload and "payer" not in str(events[0].payload)  # payload minimizado
    async with container.sessionmaker() as session:
        paid = (
            (
                await session.execute(
                    select(StateTransition).where(StateTransition.to_status == "paid")
                )
            )
            .scalars()
            .all()
        )
    assert len(paid) == 1  # liberação única do pedido
    assert (await payment_status(api, customer, order["id"]))["order_status"] == "paid"


async def test_out_of_order_events_do_not_regress_state(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order, payment = await _pix_order(api, customer)
    provider_id = payment["provider_order_id"]
    fake_mp.approve(provider_id)
    approved_snapshot = dict(fake_mp.orders[provider_id])
    params, headers, body = signed_webhook(provider_id, action="order.processed")
    await api.post(URL, params=params, headers=headers, content=body)
    await container.webhooks.process_due(10)

    # Provedor devolve um estado antigo (pendente, last_updated_date anterior) — ex.: réplica atrasada.
    fake_mp.orders[provider_id] = {
        **approved_snapshot,
        "status": "action_required",
        "status_detail": "waiting_transfer",
        "last_updated_date": "2000-01-01T00:00:00+00:00",
        "transactions": {
            "payments": [
                {
                    **approved_snapshot["transactions"]["payments"][0],
                    "status": "action_required",
                    "status_detail": "waiting_transfer",
                }
            ]
        },
    }
    params, headers, body = signed_webhook(provider_id, action="order.action_required")
    await api.post(URL, params=params, headers=headers, content=body)
    await container.webhooks.process_due(10)
    status = await payment_status(api, customer, order["id"])
    assert status["current_payment"]["status"] == "approved"
    assert status["order_status"] == "paid"


async def test_failed_processing_is_retried_then_dead_lettered_and_reprocessable(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    customer: dict[str, str],
    admin: dict[str, str],
    catalog: dict[str, Product],
) -> None:
    order, payment = await _pix_order(api, customer)
    fake_mp.approve(payment["provider_order_id"])
    fake_mp.get_failures = ["http_500"] * 100
    params, headers, body = signed_webhook(payment["provider_order_id"])
    event_id = (await api.post(URL, params=params, headers=headers, content=body)).json()[
        "event_id"
    ]

    for _ in range(container.settings.webhook_max_attempts):
        async with container.sessionmaker() as session, session.begin():
            event = await session.get(PaymentEvent, event_id)
            assert event is not None
            event.next_attempt_at = event.received_at  # antecipa o backoff
        await container.webhooks.process_due(10)
    events = await _events(container)
    assert events[0].status == EventStatus.DEAD
    assert events[0].attempts == container.settings.webhook_max_attempts
    assert "HTTP 500" in (events[0].last_error or "")

    fake_mp.get_failures = []
    forbidden = await api.post(f"/api/v1/webhooks/events/{event_id}/reprocess", headers=customer)
    assert forbidden.status_code == 403
    response = await api.post(f"/api/v1/webhooks/events/{event_id}/reprocess", headers=admin)
    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    assert (await payment_status(api, customer, order["id"]))["order_status"] == "paid"


async def test_unknown_order_and_unsupported_topics_are_ignored(
    api: httpx.AsyncClient,
    container: Container,
    fake_mp: FakeMercadoPago,
    catalog: dict[str, Product],
) -> None:
    params, headers, body = signed_webhook("ORD01UNKNOWN")
    response = await api.post(URL, params=params, headers=headers, content=body)
    assert response.status_code == 200
    await container.webhooks.process_due(10)
    params, headers, body = signed_webhook("123456")
    response = await api.post(
        URL, params={**params, "type": "payment"}, headers=headers, content=body
    )
    assert response.json()["status"] == "ignored"
    results = {e.resource_id: (e.status, e.result) for e in await _events(container)}
    assert results["ORD01UNKNOWN"] == (EventStatus.IGNORED, "order_not_found_at_provider")
    assert results["123456"] == (EventStatus.IGNORED, "unsupported_topic")


async def test_inline_background_processing(
    settings,
    container: Container,
    fake_mp: FakeMercadoPago,  # type: ignore[no-untyped-def]
    customer: dict[str, str],
    catalog: dict[str, Product],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import create_app

    inline = settings.model_copy(update={"webhook_process_inline": True})
    monkeypatch.setattr(container, "settings", inline)
    app = create_app(inline, container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as api:
        order, payment = await _pix_order(api, customer)
        fake_mp.approve(payment["provider_order_id"])
        params, headers, body = signed_webhook(payment["provider_order_id"])
        await api.post(URL, params=params, headers=headers, content=body)
        assert (await payment_status(api, customer, order["id"]))["order_status"] == "paid"
