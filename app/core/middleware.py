"""Middlewares ASGI: correlação, métricas, cabeçalhos de segurança, HTTPS e rate limiting."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.exceptions import error_response
from app.core.logging import log_fields, request_id_ctx
from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS

logger = logging.getLogger("app.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:\-]{8,128}$")
_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
]


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return str(value.decode("latin-1"))
    return None


def client_ip(scope: Scope, trust_proxy_headers: bool) -> str:
    if trust_proxy_headers:
        forwarded = _header(scope, b"x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return str(client[0]) if client else "unknown"


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    if not template:
        return "unmatched"
    # Routers incluídos ficam aninhados: o template é relativo ao prefixo (estático) do
    # router. Reconstrói o caminho completo sem usar IDs reais (cardinalidade das métricas).
    actual = [part for part in str(scope.get("path", "")).split("/") if part]
    local = [part for part in str(template).split("/") if part]
    prefix = actual[: max(len(actual) - len(local), 0)]
    return "/" + "/".join([*prefix, *local])


class RequestContextMiddleware:
    """Atribui X-Request-ID, mede latência, aplica cabeçalhos e limites de segurança."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, b"x-request-id")
        request_id = (
            incoming if incoming and _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        )
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = 1
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                headers.extend(_SECURITY_HEADERS)
                if self.settings.force_https:
                    headers.append(
                        (b"strict-transport-security", b"max-age=63072000; includeSubDomains")
                    )
                message["headers"] = headers
            await send(message)

        try:
            rejection = self._reject_reason(scope)
            if rejection is not None:
                status, code, msg = rejection
                await error_response(status, code, msg)(scope, receive, send_wrapper)
                return
            try:
                await self.app(scope, self._limited_receive(receive), send_wrapper)
            except _BodyTooLargeError:
                if status_holder.get("started"):
                    raise
                response = error_response(
                    413, "payload_too_large", "Corpo da requisição excede o limite."
                )
                await response(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            route = _route_template(scope)
            method = scope.get("method", "")
            HTTP_REQUESTS.labels(method, route, str(status_holder["status"])).inc()
            HTTP_LATENCY.labels(method, route).observe(elapsed)
            logger.info(
                "http_request",
                extra=log_fields(
                    method=method,
                    route=route,
                    status=status_holder["status"],
                    duration_ms=round(elapsed * 1000, 2),
                    client_ip=client_ip(scope, self.settings.trust_proxy_headers),
                ),
            )
            request_id_ctx.reset(token)

    def _reject_reason(self, scope: Scope) -> tuple[int, str, str] | None:
        if self.settings.force_https and not scope.get("path", "").startswith("/health"):
            scheme = scope.get("scheme", "http")
            if self.settings.trust_proxy_headers:
                scheme = _header(scope, b"x-forwarded-proto") or scheme
            if scheme != "https":
                return 403, "https_required", "HTTPS é obrigatório."
        length = _header(scope, b"content-length")
        if length is not None:
            try:
                if int(length) > self.settings.max_request_body_bytes:
                    return 413, "payload_too_large", "Corpo da requisição excede o limite."
            except ValueError:
                return 400, "bad_request", "Content-Length inválido."
        return None

    def _limited_receive(self, receive: Receive) -> Receive:
        limit = self.settings.max_request_body_bytes
        received = 0

        async def wrapped() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLargeError()
            return message

        return wrapped


class _BodyTooLargeError(Exception):
    pass


class SlidingWindowRateLimiter:
    """Limitador em memória por chave (janela deslizante de 60 s).

    Adequado para uma instância. Em produção com múltiplas réplicas, aplique o limite
    também no gateway/ingress ou substitua por um armazenamento compartilhado (ex.: Redis).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._clock = clock
        self._max_keys = max_keys

    def hit(self, key: str, limit: int, window: float = 60.0) -> float | None:
        """Registra uma requisição. Retorna segundos até liberar, se o limite foi excedido."""
        now = self._clock()
        bucket = self._hits.get(key)
        if bucket is None:
            if len(self._hits) >= self._max_keys:
                self._evict(now, window)
            bucket = self._hits.setdefault(key, deque())
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        if len(bucket) >= limit:
            return max(bucket[0] + window - now, 0.0)
        bucket.append(now)
        return None

    def _evict(self, now: float, window: float) -> None:
        for key in [k for k, b in self._hits.items() if not b or b[-1] <= now - window]:
            del self._hits[key]
        if len(self._hits) >= self._max_keys:
            self._hits.clear()


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings, limiter: SlidingWindowRateLimiter) -> None:
        self.app = app
        self.settings = settings
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path.startswith(("/health", "/metrics")):
            await self.app(scope, receive, send)
            return
        is_webhook = path.startswith("/api/v1/webhooks/mercadopago")
        limit = (
            self.settings.rate_limit_webhook_per_minute
            if is_webhook
            else self.settings.rate_limit_per_minute
        )
        ip = client_ip(scope, self.settings.trust_proxy_headers)
        retry_after = self.limiter.hit(f"{'wh' if is_webhook else 'api'}:{ip}", limit)
        if retry_after is not None:
            response = error_response(
                429,
                "rate_limited",
                "Limite de requisições excedido.",
                headers={"Retry-After": str(max(int(retry_after) + 1, 1))},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
