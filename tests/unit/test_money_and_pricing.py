from decimal import Decimal

import pytest

from app.core.exceptions import BusinessRuleError
from app.core.money import format_amount, percent_of, to_money
from app.models.product import Coupon, CouponKind
from app.services.order_service import calculate_totals, validate_coupon


def test_to_money_rounds_half_up_to_cents() -> None:
    assert to_money("10.005") == Decimal("10.01")
    assert to_money("10.004") == Decimal("10.00")
    assert to_money(Decimal("2.675")) == Decimal("2.68")  # float daria 2.67
    assert to_money(3) == Decimal("3.00")


def test_to_money_rejects_float_and_invalid_values() -> None:
    with pytest.raises(TypeError):
        to_money(0.1)  # type: ignore[arg-type]
    for invalid in ("abc", "NaN", "Infinity"):
        with pytest.raises(ValueError, match="inválido"):
            to_money(invalid)


def test_format_amount_matches_provider_contract() -> None:
    assert format_amount(Decimal("50")) == "50.00"
    assert format_amount(Decimal("1234.5")) == "1234.50"


def test_percent_of() -> None:
    assert percent_of(Decimal("99.99"), Decimal("10")) == Decimal("10.00")
    assert percent_of(Decimal("0.05"), Decimal("50")) == Decimal("0.03")


def _coupon(kind: CouponKind, value: str, **kwargs: object) -> Coupon:
    return Coupon(code="X", kind=kind, value=Decimal(value), active=True, used_count=0, **kwargs)


def test_totals_without_coupon_and_flat_shipping() -> None:
    totals = calculate_totals(
        [(Decimal("59.90"), 2), (Decimal("10.05"), 3)],
        coupon=None,
        shipping_flat_fee=Decimal("15.00"),
        free_shipping_threshold=None,
    )
    assert totals.subtotal == Decimal("149.95")
    assert totals.discount == Decimal("0.00")
    assert totals.shipping == Decimal("15.00")
    assert totals.total == Decimal("164.95")


def test_totals_percent_coupon_and_free_shipping_threshold() -> None:
    totals = calculate_totals(
        [(Decimal("100.00"), 2)],
        coupon=_coupon(CouponKind.PERCENT, "10"),
        shipping_flat_fee=Decimal("20.00"),
        free_shipping_threshold=Decimal("150.00"),
    )
    assert (totals.subtotal, totals.discount, totals.shipping, totals.total) == (
        Decimal("200.00"),
        Decimal("20.00"),
        Decimal("0.00"),
        Decimal("180.00"),
    )


def test_fixed_coupon_never_exceeds_subtotal() -> None:
    totals = calculate_totals(
        [(Decimal("3.00"), 1)],
        coupon=_coupon(CouponKind.FIXED, "5"),
        shipping_flat_fee=Decimal("0"),
        free_shipping_threshold=None,
    )
    assert totals.discount == Decimal("3.00")
    assert totals.total == Decimal("0.00")


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"active": False}, "invalid_coupon"),
        ({"max_uses": 1, "used_count": 1}, "invalid_coupon"),
        ({"min_subtotal": Decimal("500")}, "coupon_min_subtotal"),
    ],
)
def test_coupon_validation(kwargs: dict[str, object], code: str) -> None:
    from datetime import UTC, datetime

    coupon = _coupon(CouponKind.PERCENT, "10")
    for key, value in kwargs.items():
        setattr(coupon, key, value)
    with pytest.raises(BusinessRuleError) as exc:
        validate_coupon(coupon, Decimal("100"), datetime.now(UTC))
    assert exc.value.code == code


def test_missing_coupon_is_invalid() -> None:
    from datetime import UTC, datetime

    with pytest.raises(BusinessRuleError):
        validate_coupon(None, Decimal("10"), datetime.now(UTC))
