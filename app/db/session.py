from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    connect_args: dict[str, object] = {
        "server_settings": {
            "application_name": settings.app_name,
            "statement_timeout": str(settings.db_statement_timeout_ms),
            # Evita transações esquecidas segurando locks indefinidamente.
            "idle_in_transaction_session_timeout": "30000",
        }
    }
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objetos continuam legíveis após o commit das transações curtas.
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
