from __future__ import annotations

from typing import Any

import httpx

from tests.conftest import idem


async def create_order(
    api: httpx.AsyncClient,
    headers: dict[str, str],
    items: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body = {"items": items or [{"sku": "SKU-A", "quantity": 2}], **extra}
    response = await api.post("/api/v1/orders", json=body, headers={**headers, **idem()})
    assert response.status_code == 201, response.text
    return dict(response.json())


async def pay(
    api: httpx.AsyncClient,
    headers: dict[str, str],
    order_id: str,
    payload: dict[str, Any],
    key: dict[str, str] | None = None,
) -> httpx.Response:
    return await api.post(
        f"/api/v1/orders/{order_id}/payments", json=payload, headers={**headers, **(key or idem())}
    )


async def payment_status(
    api: httpx.AsyncClient, headers: dict[str, str], order_id: str, refresh: bool = False
) -> dict[str, Any]:
    response = await api.get(
        f"/api/v1/orders/{order_id}/payment-status",
        params={"refresh": str(refresh).lower()},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return dict(response.json())
