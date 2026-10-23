import pytest

from app.domain.states import (
    ORDER_TRANSITIONS,
    PAYMENT_TRANSITIONS,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    can_transition_order,
    can_transition_payment,
    can_transition_refund,
    order_status_for_payment,
)
from app.integrations.mercadopago.payments import (
    extract_payment_data,
    map_order_payment_status,
    map_payment_status,
    map_refund_status,
)
from app.integrations.mercadopago.schemas import MPOrder, MPPayment, MPPaymentMethod

S = PaymentStatus


def test_every_status_has_transition_rules() -> None:
    assert set(PAYMENT_TRANSITIONS) == set(PaymentStatus)
    assert set(ORDER_TRANSITIONS) == set(OrderStatus)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.APPROVED, S.PENDING),  # webhook antigo não regride pagamento aprovado
        (S.APPROVED, S.AUTHORIZED),
        (S.REFUNDED, S.APPROVED),
        (S.REJECTED, S.APPROVED),
        (S.CANCELLED, S.PENDING),
        (S.PARTIALLY_REFUNDED, S.APPROVED),
    ],
)
def test_regressions_are_forbidden(current: PaymentStatus, target: PaymentStatus) -> None:
    assert not can_transition_payment(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.CREATED, S.PENDING),
        (S.UNKNOWN, S.APPROVED),
        (S.PENDING, S.APPROVED),
        (S.AUTHORIZED, S.APPROVED),  # captura
        (S.APPROVED, S.PARTIALLY_REFUNDED),
        (S.PARTIALLY_REFUNDED, S.REFUNDED),
        (S.PENDING, S.PENDING),
    ],
)
def test_allowed_payment_transitions(current: PaymentStatus, target: PaymentStatus) -> None:
    assert can_transition_payment(current, target)


def test_order_transitions() -> None:
    assert can_transition_order(OrderStatus.PROCESSING, OrderStatus.PAID)
    assert can_transition_order(OrderStatus.PROCESSING, OrderStatus.PENDING_PAYMENT)
    assert not can_transition_order(OrderStatus.PAID, OrderStatus.PENDING_PAYMENT)
    assert not can_transition_order(OrderStatus.CANCELLED, OrderStatus.PAID)
    assert not can_transition_order(OrderStatus.REFUNDED, OrderStatus.PAID)


def test_refund_transitions() -> None:
    assert can_transition_refund(RefundStatus.PENDING, RefundStatus.APPROVED)
    assert not can_transition_refund(RefundStatus.APPROVED, RefundStatus.PENDING)


def test_authorized_is_not_paid() -> None:
    assert order_status_for_payment(S.AUTHORIZED) == OrderStatus.PROCESSING
    assert order_status_for_payment(S.APPROVED) == OrderStatus.PAID
    assert order_status_for_payment(S.REJECTED) == OrderStatus.PENDING_PAYMENT
    assert order_status_for_payment(S.EXPIRED) == OrderStatus.PENDING_PAYMENT


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        ("created", "created", S.PENDING),
        ("processing", "in_process", S.PENDING),
        ("processing", "in_review", S.IN_REVIEW),
        ("processing", "pending_review_manual", S.IN_REVIEW),
        ("in_review", "in_review", S.IN_REVIEW),
        ("action_required", "waiting_transfer", S.PENDING),
        ("action_required", "waiting_payment", S.PENDING),
        ("action_required", "pending_challenge", S.PENDING),
        ("action_required", "waiting_capture", S.AUTHORIZED),
        ("processed", "accredited", S.APPROVED),
        ("processed", "partially_refunded", S.PARTIALLY_REFUNDED),
        ("processed", "refunded", S.REFUNDED),
        ("refunded", "refunded", S.REFUNDED),
        ("failed", "rejected_by_issuer", S.REJECTED),
        ("failed", "expired", S.EXPIRED),
        ("canceled", "expired", S.EXPIRED),
        ("expired", "expired", S.EXPIRED),
        ("canceled", "canceled", S.CANCELLED),
        ("charged_back", "settled", S.CHARGED_BACK),
    ],
)
def test_provider_status_mapping(status: str, detail: str, expected: PaymentStatus) -> None:
    assert map_payment_status(status, detail) == expected


@pytest.mark.parametrize(
    ("status", "detail"), [("processed", "weird"), ("mystery", "x"), (None, None)]
)
def test_undocumented_provider_status_is_not_guessed(
    status: str | None, detail: str | None
) -> None:
    assert map_payment_status(status, detail) is None


def test_order_mapping_prefers_transaction_status() -> None:
    order = MPOrder(
        id="ORD1",
        status="processing",
        status_detail="in_process",
        transactions={
            "payments": [MPPayment(id="PAY1", status="processed", status_detail="accredited")]
        },  # type: ignore[arg-type]
    )
    assert map_order_payment_status(order)[0] == S.APPROVED


def test_refund_status_mapping() -> None:
    assert map_refund_status("processed") == RefundStatus.APPROVED
    assert map_refund_status("in_process") == RefundStatus.PENDING
    assert map_refund_status("failed") == RefundStatus.REJECTED


def test_extract_payment_data_excludes_card_data() -> None:
    payment = MPPayment(
        id="PAY1",
        payment_method=MPPaymentMethod(
            id="pix", type="bank_transfer", qr_code="000201", ticket_url="u"
        ),
    )
    assert extract_payment_data(payment) == {"qr_code": "000201", "ticket_url": "u"}
    card = MPPayment(
        id="PAY2", payment_method=MPPaymentMethod(id="master", type="credit_card", installments=3)
    )
    assert extract_payment_data(card) is None
