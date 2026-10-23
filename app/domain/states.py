"""Estados internos e transições permitidas.

As transições são verificadas antes de qualquer atualização, o que impede que
notificações antigas ou fora de ordem provoquem regressões (ex.: `approved` → `pending`).
Atualizações para o mesmo estado são permitidas para atualizar detalhes do provedor.
"""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"  # aguardando início de pagamento (ou nova tentativa)
    PROCESSING = "processing"  # há uma tentativa de pagamento ativa
    PAID = "paid"  # pagamento aprovado e creditado (processed/accredited)
    COMPLETED = "completed"  # pedido entregue/concluído pelo fluxo de fulfillment
    CANCELLED = "cancelled"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    CHARGED_BACK = "charged_back"


class PaymentStatus(StrEnum):
    CREATED = "created"  # registrado localmente, ainda não confirmado pelo provedor
    UNKNOWN = "unknown"  # chamada ao provedor sem resultado conhecido (timeout etc.)
    PENDING = "pending"  # aguardando ação do pagador ou processamento (Pix/boleto/3DS)
    IN_REVIEW = "in_review"  # análise de risco/manual no provedor
    AUTHORIZED = "authorized"  # cartão autorizado, aguardando captura
    APPROVED = "approved"  # creditado
    REJECTED = "rejected"  # recusado pelo provedor/emissor
    FAILED = "failed"  # não criado no provedor (erro definitivo antes da cobrança)
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    CHARGED_BACK = "charged_back"


class RefundStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class PaymentMethod(StrEnum):
    PIX = "pix"
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    BOLETO = "boleto"


class EventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"  # falhou, nova tentativa agendada
    DEAD = "dead"  # tentativas esgotadas; requer intervenção


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


ACTIVE_PAYMENT_STATUSES = frozenset(
    {
        PaymentStatus.CREATED,
        PaymentStatus.UNKNOWN,
        PaymentStatus.PENDING,
        PaymentStatus.IN_REVIEW,
        PaymentStatus.AUTHORIZED,
    }
)
SETTLED_PAYMENT_STATUSES = frozenset({PaymentStatus.APPROVED, PaymentStatus.PARTIALLY_REFUNDED})
UNSUCCESSFUL_PAYMENT_STATUSES = frozenset(
    {PaymentStatus.REJECTED, PaymentStatus.FAILED, PaymentStatus.CANCELLED, PaymentStatus.EXPIRED}
)

_PRE_PROVIDER = {
    PaymentStatus.PENDING,
    PaymentStatus.IN_REVIEW,
    PaymentStatus.AUTHORIZED,
    PaymentStatus.APPROVED,
    PaymentStatus.REJECTED,
    PaymentStatus.CANCELLED,
    PaymentStatus.EXPIRED,
}

PAYMENT_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset({*_PRE_PROVIDER, PaymentStatus.UNKNOWN, PaymentStatus.FAILED}),
    PaymentStatus.UNKNOWN: frozenset({*_PRE_PROVIDER, PaymentStatus.FAILED}),
    PaymentStatus.PENDING: frozenset(
        {
            PaymentStatus.IN_REVIEW,
            PaymentStatus.AUTHORIZED,
            PaymentStatus.APPROVED,
            PaymentStatus.REJECTED,
            PaymentStatus.CANCELLED,
            PaymentStatus.EXPIRED,
        }
    ),
    PaymentStatus.IN_REVIEW: frozenset(
        {
            PaymentStatus.PENDING,
            PaymentStatus.AUTHORIZED,
            PaymentStatus.APPROVED,
            PaymentStatus.REJECTED,
            PaymentStatus.CANCELLED,
        }
    ),
    PaymentStatus.AUTHORIZED: frozenset(
        {
            PaymentStatus.APPROVED,
            PaymentStatus.CANCELLED,
            PaymentStatus.EXPIRED,
            PaymentStatus.REJECTED,
        }
    ),
    PaymentStatus.APPROVED: frozenset(
        {PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED, PaymentStatus.CHARGED_BACK}
    ),
    PaymentStatus.PARTIALLY_REFUNDED: frozenset(
        {PaymentStatus.REFUNDED, PaymentStatus.CHARGED_BACK}
    ),
    PaymentStatus.REJECTED: frozenset(),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.CANCELLED: frozenset(),
    PaymentStatus.EXPIRED: frozenset(),
    PaymentStatus.REFUNDED: frozenset(),
    PaymentStatus.CHARGED_BACK: frozenset(),
}

ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_PAYMENT: frozenset(
        {OrderStatus.PROCESSING, OrderStatus.PAID, OrderStatus.CANCELLED}
    ),
    OrderStatus.PROCESSING: frozenset(
        {OrderStatus.PENDING_PAYMENT, OrderStatus.PAID, OrderStatus.CANCELLED}
    ),
    OrderStatus.PAID: frozenset(
        {
            OrderStatus.COMPLETED,
            OrderStatus.PARTIALLY_REFUNDED,
            OrderStatus.REFUNDED,
            OrderStatus.CHARGED_BACK,
        }
    ),
    OrderStatus.COMPLETED: frozenset(
        {OrderStatus.PARTIALLY_REFUNDED, OrderStatus.REFUNDED, OrderStatus.CHARGED_BACK}
    ),
    OrderStatus.PARTIALLY_REFUNDED: frozenset({OrderStatus.REFUNDED, OrderStatus.CHARGED_BACK}),
    OrderStatus.REFUNDED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.CHARGED_BACK: frozenset(),
}

REFUND_TRANSITIONS: dict[RefundStatus, frozenset[RefundStatus]] = {
    RefundStatus.PENDING: frozenset(
        {RefundStatus.APPROVED, RefundStatus.REJECTED, RefundStatus.FAILED}
    ),
    RefundStatus.APPROVED: frozenset(),
    RefundStatus.REJECTED: frozenset(),
    RefundStatus.FAILED: frozenset(),
}


def can_transition_payment(current: PaymentStatus, target: PaymentStatus) -> bool:
    return current == target or target in PAYMENT_TRANSITIONS[current]


def can_transition_order(current: OrderStatus, target: OrderStatus) -> bool:
    return current == target or target in ORDER_TRANSITIONS[current]


def can_transition_refund(current: RefundStatus, target: RefundStatus) -> bool:
    return current == target or target in REFUND_TRANSITIONS[current]


def order_status_for_payment(payment_status: PaymentStatus) -> OrderStatus:
    """Estado de pedido desejado a partir do estado do seu pagamento corrente."""
    if payment_status in ACTIVE_PAYMENT_STATUSES:
        return OrderStatus.PROCESSING
    if payment_status == PaymentStatus.APPROVED:
        return OrderStatus.PAID
    if payment_status == PaymentStatus.PARTIALLY_REFUNDED:
        return OrderStatus.PARTIALLY_REFUNDED
    if payment_status == PaymentStatus.REFUNDED:
        return OrderStatus.REFUNDED
    if payment_status == PaymentStatus.CHARGED_BACK:
        return OrderStatus.CHARGED_BACK
    return OrderStatus.PENDING_PAYMENT  # recusado/expirado/cancelado: permite nova tentativa
