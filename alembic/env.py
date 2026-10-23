from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.db.base import Base
from app.models import *  # noqa: F403  (registra todas as tabelas no metadata)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    # Permite que testes/scripts injetem a URL sem exigir todas as configurações da aplicação.
    url = config.attributes.get("database_url") or os.environ.get("DATABASE_URL")
    if url:
        return str(url)
    from app.core.config import get_settings  # noqa: PLC0415  (só quando necessário)

    return get_settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url = make_url(_database_url())
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        try:
            connection = await engine.connect()
        except (DBAPIError, OSError) as exc:
            # No Windows o asyncpg mascara "database does not exist" como conexão encerrada.
            target = f"'{url.database}' em {url.host}:{url.port} (usuário {url.username})"
            raise SystemExit(
                f"Não foi possível conectar ao banco {target}. Verifique se o PostgreSQL está "
                f"ativo, se o banco existe (CREATE DATABASE {url.database};) e as credenciais "
                f"em DATABASE_URL. Detalhe: {type(getattr(exc, 'orig', exc)).__name__}"
            ) from None
        try:
            await connection.run_sync(_run)
        finally:
            await connection.close()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
