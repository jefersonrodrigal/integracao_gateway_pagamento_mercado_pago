"""Cliente HTTPX assíncrono e tipado para a Orders API do Mercado Pago.

O SDK oficial Python é síncrono; para não bloquear o event loop e ter controle explícito
de timeouts, retries e idempotência, as chamadas são feitas diretamente com HTTPX.

Política de retry:
- Leituras (GET): retry com backoff exponencial + jitter em falhas de rede, 429 e 5xx.
- Escritas (POST com X-Idempotency-Key): retry apenas reutilizando a MESMA chave. Se alguma
  tentativa pode ter chegado ao provedor (timeout, 5xx, 423) e não houve resposta definitiva,
  levanta `MercadoPagoOutcomeUnknownError` para que o chamador reconcilie em vez de recriar.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import log_fields
from app.core.metrics import PROVIDER_LATENCY, PROVIDER_REQUESTS
from app.integrations.mercadopago.exceptions import (
    MercadoPagoAPIError,
    MercadoPagoOutcomeUnknownError,
    MercadoPagoUnavailableError,
)
from app.integrations.mercadopago.schemas import (
    MPOrder,
    MPOrderSearchResult,
    MPPaymentMethodInfo,
)

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# 423 resource_locked: a chave idempotente está em uso por uma requisição em andamento.
MAYBE_EXECUTED_STATUS = frozenset({423, 500, 502, 503, 504})

SleepFn = Callable[[float], Awaitable[None]]


def _error_code(body: Any) -> tuple[str | None, str]:
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            first = errors[0]
            return first.get("code"), str(first.get("message") or "")
        code = body.get("code") or body.get("error")
        return (str(code) if code else None), str(body.get("message") or "")
    return None, ""


class MercadoPagoClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=settings.mercado_pago_base_url,
            timeout=httpx.Timeout(
                settings.mercado_pago_timeout_seconds,
                connect=settings.mercado_pago_connect_timeout_seconds,
            ),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            headers={
                "Authorization": f"Bearer {settings.mercado_pago_access_token.get_secret_value()}",
                "Accept": "application/json",
                "User-Agent": f"{settings.app_name}/0.1",
            },
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ Orders
    async def create_order(self, payload: dict[str, Any], idempotency_key: str) -> MPOrder:
        body = await self._write("create_order", "/v1/orders", idempotency_key, payload)
        return MPOrder.model_validate(body)

    async def get_order(self, order_id: str) -> MPOrder:
        body = await self._read("get_order", f"/v1/orders/{_safe_id(order_id)}")
        return MPOrder.model_validate(body)

    async def search_orders_by_reference(
        self, external_reference: str, begin: datetime, end: datetime
    ) -> list[MPOrder]:
        params = {
            "external_reference": external_reference,
            "begin_date": begin.isoformat(timespec="seconds"),
            "end_date": end.isoformat(timespec="seconds"),
        }
        body = await self._read("search_orders", "/v1/orders", params=params)
        return MPOrderSearchResult.model_validate(body).data

    async def cancel_order(self, order_id: str, idempotency_key: str) -> MPOrder:
        body = await self._write(
            "cancel_order", f"/v1/orders/{_safe_id(order_id)}/cancel", idempotency_key, None
        )
        return MPOrder.model_validate(body)

    async def capture_order(self, order_id: str, idempotency_key: str) -> MPOrder:
        body = await self._write(
            "capture_order", f"/v1/orders/{_safe_id(order_id)}/capture", idempotency_key, None
        )
        return MPOrder.model_validate(body)

    async def refund_order(
        self,
        order_id: str,
        idempotency_key: str,
        transactions: list[dict[str, str]] | None = None,
    ) -> MPOrder:
        """Reembolso total (sem corpo) ou parcial (`transactions=[{"id","amount"}]`)."""
        payload = {"transactions": transactions} if transactions else None
        body = await self._write(
            "refund_order", f"/v1/orders/{_safe_id(order_id)}/refund", idempotency_key, payload
        )
        return MPOrder.model_validate(body)

    # --------------------------------------------------------- Payment methods
    async def list_payment_methods(self) -> list[MPPaymentMethodInfo]:
        body = await self._read("list_payment_methods", "/v1/payment_methods")
        if not isinstance(body, list):
            raise MercadoPagoUnavailableError(
                "resposta inesperada", operation="list_payment_methods"
            )
        return [MPPaymentMethodInfo.model_validate(item) for item in body]

    # ---------------------------------------------------------------- Internals
    async def _read(
        self, operation: str, path: str, *, params: dict[str, str] | None = None
    ) -> Any:
        attempts = self._settings.mercado_pago_max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = await self._send(operation, "GET", path, params=params)
            except httpx.TransportError as exc:
                if attempt == attempts:
                    raise MercadoPagoUnavailableError(
                        type(exc).__name__, operation=operation
                    ) from None
                await self._backoff(attempt, None)
                continue
            if response.status_code in RETRYABLE_STATUS and attempt < attempts:
                await self._backoff(attempt, response)
                continue
            if response.status_code in RETRYABLE_STATUS:
                raise MercadoPagoUnavailableError(
                    f"HTTP {response.status_code}", operation=operation
                )
            return self._parse(operation, response)
        raise MercadoPagoUnavailableError("tentativas esgotadas", operation=operation)

    async def _write(
        self, operation: str, path: str, idempotency_key: str, payload: dict[str, Any] | None
    ) -> Any:
        if not 1 <= len(idempotency_key) <= 128:
            raise ValueError("X-Idempotency-Key deve ter de 1 a 128 caracteres")
        attempts = self._settings.mercado_pago_max_retries + 1
        maybe_executed = False
        last_status: int | None = None
        last_code: str | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self._send(
                    operation,
                    "POST",
                    path,
                    json=payload,
                    headers={"X-Idempotency-Key": idempotency_key},
                )
            except httpx.TransportError as exc:
                # ConnectError/ConnectTimeout: a requisição não saiu; os demais podem ter chegado.
                if not isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
                    maybe_executed = True
                last_status, last_code = None, type(exc).__name__
                if attempt < attempts:
                    await self._backoff(attempt, None)
                    continue
                break

            status = response.status_code
            if status == 409:
                code, _ = _error_code(_json_or_none(response))
                if code == "idempotency_key_already_used":
                    # A mesma chave já foi processada: a operação original existe.
                    raise MercadoPagoOutcomeUnknownError(
                        operation=operation, reason=code, status_code=status, code=code
                    )
            if status in RETRYABLE_STATUS or status == 423:
                maybe_executed = maybe_executed or status in MAYBE_EXECUTED_STATUS
                last_status = status
                last_code, _ = _error_code(_json_or_none(response))
                if attempt < attempts:
                    await self._backoff(attempt, response)
                    continue
                break
            return self._parse(operation, response)

        if maybe_executed:
            raise MercadoPagoOutcomeUnknownError(
                operation=operation,
                reason=last_code or f"HTTP {last_status}",
                status_code=last_status,
                code=last_code,
            )
        raise MercadoPagoUnavailableError(last_code or f"HTTP {last_status}", operation=operation)

    async def _send(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        started = time.perf_counter()
        try:
            response = await self._http.request(
                method, path, params=params, json=json, headers=headers
            )
        except httpx.TimeoutException:
            PROVIDER_REQUESTS.labels(operation, "timeout").inc()
            logger.warning("mercadopago_timeout", extra=log_fields(operation=operation))
            raise
        except httpx.TransportError as exc:
            PROVIDER_REQUESTS.labels(operation, "network_error").inc()
            logger.warning(
                "mercadopago_network_error",
                extra=log_fields(operation=operation, error=type(exc).__name__),
            )
            raise
        finally:
            PROVIDER_LATENCY.labels(operation).observe(time.perf_counter() - started)
        outcome = "success" if response.is_success else f"http_{response.status_code}"
        PROVIDER_REQUESTS.labels(operation, outcome).inc()
        logger.info(
            "mercadopago_response",
            extra=log_fields(
                operation=operation,
                status=response.status_code,
                provider_request_id=response.headers.get("x-request-id"),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            ),
        )
        return response

    def _parse(self, operation: str, response: httpx.Response) -> Any:
        body = _json_or_none(response)
        if response.is_success:
            if body is None:
                raise MercadoPagoUnavailableError("resposta sem JSON", operation=operation)
            return body
        code, message = _error_code(body)
        raise MercadoPagoAPIError(
            operation=operation,
            status_code=response.status_code,
            code=code,
            message=message,
            payload=body if isinstance(body, dict) else None,
            provider_request_id=response.headers.get("x-request-id"),
        )

    async def _backoff(self, attempt: int, response: httpx.Response | None) -> None:
        base = self._settings.mercado_pago_retry_base_delay_seconds
        cap = self._settings.mercado_pago_retry_max_delay_seconds
        delay = min(cap, base * (2 ** (attempt - 1)))
        delay = random.uniform(0, delay)  # noqa: S311  (full jitter; não criptográfico)
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                delay = min(cap, max(delay, float(retry_after)))
        await self._sleep(delay)


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _safe_id(value: str) -> str:
    """Impede path traversal/injeção ao interpolar IDs externos na URL."""
    if not value or not value.replace("_", "").replace("-", "").isalnum() or len(value) > 64:
        raise ValueError("identificador externo inválido")
    return value
