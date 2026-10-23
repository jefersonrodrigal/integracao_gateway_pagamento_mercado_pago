"""Regras monetárias.

- Todos os valores são `Decimal`; `float` é rejeitado.
- Precisão: 2 casas decimais (centavos), moeda BRL.
- Arredondamento: ROUND_HALF_UP aplicado uma única vez por linha de item, desconto e frete.
- Envio ao Mercado Pago: string com 2 casas ("50.00"), conforme os exemplos oficiais.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def to_money(value: Decimal | int | str) -> Decimal:
    if isinstance(value, float):
        raise TypeError("valores monetários não podem ser float")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("valor monetário inválido") from exc
    if not amount.is_finite():
        raise ValueError("valor monetário inválido")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def percent_of(amount: Decimal, percent: Decimal) -> Decimal:
    return to_money(amount * percent / Decimal(100))


def format_amount(amount: Decimal) -> str:
    """Serializa para o formato da Orders API (ex.: "50.00")."""
    return f"{to_money(amount):.2f}"
