import asyncio

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import command
from app.db.base import Base
from tests.conftest import TEST_DATABASE_URL, alembic_config

pytestmark = pytest.mark.integration
MIGRATION_DB = "payments_migration_check"


async def test_migrations_upgrade_downgrade_and_match_models(engine: AsyncEngine) -> None:
    admin_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DB}"))
        await conn.execute(text(f"CREATE DATABASE {MIGRATION_DB}"))
    url = TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{MIGRATION_DB}"
    target = create_async_engine(url)
    try:
        config = alembic_config(url)
        await asyncio.to_thread(command.upgrade, config, "head")

        def diff(sync_conn):  # type: ignore[no-untyped-def]
            context = MigrationContext.configure(sync_conn, opts={"compare_type": True})
            return compare_metadata(context, Base.metadata)

        def tables(sync_conn):  # type: ignore[no-untyped-def]
            return set(inspect(sync_conn).get_table_names())

        async with target.connect() as conn:
            assert await conn.run_sync(diff) == []  # migrations == modelos
            assert {"orders", "payments", "payment_events", "refunds", "idempotency_records"} <= (
                await conn.run_sync(tables)
            )
        await asyncio.to_thread(command.downgrade, config, "base")
        async with target.connect() as conn:
            assert await conn.run_sync(tables) == {"alembic_version"}
        await asyncio.to_thread(command.upgrade, config, "head")
    finally:
        await target.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DB} WITH (FORCE)"))
        await admin.dispose()
