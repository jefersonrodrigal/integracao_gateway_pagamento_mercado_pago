from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.models.product import Coupon, Product


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, order_id: uuid.UUID, *, for_update: bool = False) -> Order | None:
        stmt = select(Order).where(Order.id == order_id)
        if for_update:
            # Lock apenas da linha do pedido (os itens são carregados depois via selectin).
            stmt = stmt.with_for_update(of=Order).execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def add(self, order: Order, items: Sequence[OrderItem]) -> None:
        order.items = list(items)
        self.session.add(order)

    async def lock_products(self, skus: Sequence[str]) -> list[Product]:
        # Ordem determinística de lock evita deadlocks entre pedidos concorrentes.
        stmt = (
            select(Product)
            .where(Product.sku.in_(skus))
            .order_by(Product.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def lock_products_by_id(self, product_ids: Sequence[uuid.UUID]) -> list[Product]:
        stmt = (
            select(Product)
            .where(Product.id.in_(product_ids))
            .order_by(Product.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def lock_coupon(self, code: str) -> Coupon | None:
        stmt = (
            select(Coupon)
            .where(Coupon.code == code)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
