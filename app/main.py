"""Aplicação FastAPI.

uvicorn app.main:app
uvicorn --factory app.main:create_app
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.api.v1.router import api_router
from app.container import Container
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import (
    RateLimitMiddleware,
    RequestContextMiddleware,
    SlidingWindowRateLimiter,
)

DESCRIPTION = """
API de pagamentos integrada ao **Mercado Pago Checkout Transparente (Orders API)**.

* Autenticação: `Authorization: Bearer <JWT>` (papéis `customer` e `admin`).
* Operações críticas exigem o cabeçalho `Idempotency-Key`.
* Valores monetários são strings decimais com 2 casas (ex.: `"150.00"`), moeda BRL.
* Erros seguem o formato `{"error": {"code", "message", "details", "request_id"}}`.
"""


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        app_name=settings.app_name,
        environment=settings.app_env,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        app.state.container = container or Container.build(settings)
        try:
            yield
        finally:
            if owned:
                await app.state.container.aclose()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    if container is not None:
        app.state.container = container

    register_exception_handlers(app)
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Idempotent-Replayed"],
            allow_credentials=False,
            max_age=600,
        )
    app.add_middleware(RateLimitMiddleware, settings=settings, limiter=SlidingWindowRateLimiter())
    app.add_middleware(RequestContextMiddleware, settings=settings)

    app.include_router(health.router)
    app.include_router(api_router)
    return app


_default_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    # `uvicorn app.main:app`: criada sob demanda, para que importar este módulo
    # (testes, scripts) não exija configuração completa.
    global _default_app  # noqa: PLW0603
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
