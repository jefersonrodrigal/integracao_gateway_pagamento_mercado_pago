"""Popula o catálogo para desenvolvimento/homologação: `python -m app.scripts.seed`."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.db.session import create_engine, create_sessionmaker
from app.models.product import Coupon, CouponKind, Product

PRODUCTS = [
    {
        "sku": "CAMISETA-P",
        "name": "Camiseta básica P",
        "unit_price": Decimal("59.90"),
        "stock": 100,
    },
    {"sku": "CANECA-01", "name": "Caneca de cerâmica", "unit_price": Decimal("34.50"), "stock": 50},
    {"sku": "LIVRO-PY", "name": "Livro de Python", "unit_price": Decimal("120.00"), "stock": 20},
]
COUPONS = [
    {"code": "BEMVINDO10", "kind": CouponKind.PERCENT, "value": Decimal("10.00")},
    {"code": "DESCONTO5", "kind": CouponKind.FIXED, "value": Decimal("5.00")},
]


async def main() -> None:
    engine = create_engine(get_settings())
    sessionmaker = create_sessionmaker(engine)
    async with sessionmaker() as session, session.begin():
        for product in PRODUCTS:
            await session.execute(
                insert(Product).values(**product).on_conflict_do_nothing(index_elements=["sku"])
            )
        for coupon in COUPONS:
            await session.execute(
                insert(Coupon).values(**coupon).on_conflict_do_nothing(index_elements=["code"])
            )
    await engine.dispose()
    print(f"Seed concluído: {len(PRODUCTS)} produtos, {len(COUPONS)} cupons.")


if __name__ == "__main__":
    asyncio.run(main())
