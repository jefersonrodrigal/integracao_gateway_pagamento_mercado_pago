"""Pedidos: cálculo de valores no servidor, reserva de estoque, consulta e liberação."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.core.logging import log_fields
from app.core.money import ZERO, percent_of, to_money
from app.core.security import Principal
from app.domain.states import OrderStatus, can_transition_order
from app.models.order import Order, OrderItem
from app.models.product import Coupon, CouponKind
from app.repositories.audit import AuditRepository
from app.repositories.orders import OrderRepository
from app.schemas.orders import OrderCreate

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OrderTotals:
    subtotal: Decimal
    discount: Decimal
    shipping: Decimal
    total: Decimal


def calculate_totals(
    lines: Sequence[tuple[Decimal, int]],
    *,
    coupon: Coupon | None,
    shipping_flat_fee: Decimal,
    free_shipping_threshold: Decimal | None,
) -> OrderTotals:
    """Calcula os totais. Cada linha e cada componente é arredondado (HALF_UP) em centavos."""
    subtotal = to_money(sum((to_money(price * qty) for price, qty in lines), ZERO))
    discount = ZERO
    if coupon is not None:
        if coupon.kind == CouponKind.PERCENT:
            discount = percent_of(subtotal, coupon.value)
        else:
            discount = to_money(coupon.value)
        discount = min(discount, subtotal)
    discounted = subtotal - discount
    shipping = to_money(shipping_flat_fee)
    if free_shipping_threshold is not None and discounted >= free_shipping_threshold:
        shipping = ZERO
    return OrderTotals(subtotal, discount, shipping, to_money(discounted + shipping))


def validate_coupon(coupon: Coupon | None, subtotal: Decimal, now: datetime) -> Coupon:
    if coupon is None or not coupon.active:
        raise BusinessRuleError("Cupom inválido.", code="invalid_coupon")
    if coupon.valid_from and now < coupon.valid_from:
        raise BusinessRuleError("Cupom ainda não está vigente.", code="invalid_coupon")
    if coupon.valid_until and now > coupon.valid_until:
        raise BusinessRuleError("Cupom expirado.", code="invalid_coupon")
    if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
        raise BusinessRuleError("Cupom esgotado.", code="invalid_coupon")
    if coupon.min_subtotal is not None and subtotal < coupon.min_subtotal:
        raise BusinessRuleError(
            "Subtotal abaixo do mínimo exigido pelo cupom.", code="coupon_min_subtotal"
        )
    return coupon


def ensure_can_access(principal: Principal, order: Order | None) -> Order:
    # 404 (e não 403) para pedidos de terceiros: não revela a existência do recurso (IDOR).
    if order is None or not principal.can_access_customer(order.customer_id):
        raise NotFoundError("Pedido não encontrado.", code="order_not_found")
    return order


def set_order_status(
    session: AsyncSession, order: Order, target: OrderStatus, *, source: str, reason: str
) -> bool:
    if order.status == target:
        return False
    if not can_transition_order(order.status, target):
        raise ConflictError(
            "Transição de estado do pedido não permitida.",
            code="invalid_order_transition",
            details={"from": order.status.value, "to": target.value},
        )
    previous = order.status
    order.status = target
    now = datetime.now(UTC)
    if target == OrderStatus.PAID and order.paid_at is None:
        order.paid_at = now
    if target == OrderStatus.CANCELLED:
        order.cancelled_at = now
    AuditRepository(session).record(
        entity_type="order",
        entity_id=order.id,
        from_status=previous.value,
        to_status=target.value,
        source=source,
        reason=reason,
    )
    logger.info(
        "order_status_changed",
        extra=log_fields(
            order_id=str(order.id), source=source, to=target.value, frm=previous.value
        ),
    )
    return True


async def cancel_order_locally(
    session: AsyncSession, order: Order, *, source: str, reason: str
) -> None:
    """Cancela o pedido e devolve estoque e uso de cupom (na mesma transação)."""
    repo = OrderRepository(session)
    set_order_status(session, order, OrderStatus.CANCELLED, source=source, reason=reason)
    quantities = {item.product_id: item.quantity for item in order.items}
    for product in await repo.lock_products_by_id(list(quantities)):
        product.stock += quantities[product.id]
    if order.coupon_code:
        coupon = await repo.lock_coupon(order.coupon_code)
        if coupon is not None and coupon.used_count > 0:
            coupon.used_count -= 1


class OrderService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings

    async def create_order(self, principal: Principal, data: OrderCreate) -> Order:
        quantities = {item.sku: item.quantity for item in data.items}
        async with self._sessionmaker() as session, session.begin():
            repo = OrderRepository(session)
            products = {p.sku: p for p in await repo.lock_products(list(quantities))}

            missing = sorted(
                sku for sku in quantities if sku not in products or not products[sku].active
            )
            if missing:
                raise BusinessRuleError(
                    "Produto indisponível.", code="product_unavailable", details={"skus": missing}
                )
            short = sorted(sku for sku, qty in quantities.items() if products[sku].stock < qty)
            if short:
                raise ConflictError(
                    "Estoque insuficiente.", code="insufficient_stock", details={"skus": short}
                )

            lines = [(products[sku].unit_price, qty) for sku, qty in quantities.items()]
            subtotal = calculate_totals(
                lines, coupon=None, shipping_flat_fee=ZERO, free_shipping_threshold=None
            ).subtotal
            coupon = None
            if data.coupon_code:
                coupon = validate_coupon(
                    await repo.lock_coupon(data.coupon_code.upper()), subtotal, datetime.now(UTC)
                )
            totals = calculate_totals(
                lines,
                coupon=coupon,
                shipping_flat_fee=self._settings.shipping_flat_fee,
                free_shipping_threshold=self._settings.free_shipping_threshold,
            )
            if totals.total <= ZERO:
                raise BusinessRuleError("O valor total deve ser positivo.", code="invalid_total")
            if totals.total > self._settings.max_order_amount:
                raise BusinessRuleError(
                    "Valor do pedido acima do limite permitido.", code="order_amount_exceeded"
                )

            order = Order(
                id=uuid.uuid4(),
                customer_id=principal.subject,
                reference=data.reference,
                status=OrderStatus.PENDING_PAYMENT,
                currency=self._settings.currency,
                subtotal=totals.subtotal,
                discount_total=totals.discount,
                shipping_total=totals.shipping,
                total_amount=totals.total,
                coupon_code=coupon.code if coupon else None,
            )
            items = []
            for sku, qty in quantities.items():
                product = products[sku]
                product.stock -= qty
                items.append(
                    OrderItem(
                        product_id=product.id,
                        sku=product.sku,
                        name=product.name,
                        unit_price=product.unit_price,
                        quantity=qty,
                        line_total=to_money(product.unit_price * qty),
                    )
                )
            if coupon is not None:
                coupon.used_count += 1
            repo.add(order, items)
            AuditRepository(session).record(
                entity_type="order",
                entity_id=order.id,
                from_status=None,
                to_status=order.status.value,
                source="api",
                reason="order_created",
            )
        logger.info(
            "order_created",
            extra=log_fields(
                order_id=str(order.id), total=str(order.total_amount), items=len(items)
            ),
        )
        return order

    async def get_order(self, principal: Principal, order_id: uuid.UUID) -> Order:
        async with self._sessionmaker() as session:
            return ensure_can_access(principal, await OrderRepository(session).get(order_id))
