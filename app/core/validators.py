"""Validadores de documentos brasileiros (CPF/CNPJ) usados no boleto."""

from __future__ import annotations


def only_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _check_digit(digits: str, weights: list[int]) -> int:
    total = sum(int(d) * w for d, w in zip(digits, weights, strict=True))
    remainder = total % 11
    return 0 if remainder < 2 else 11 - remainder


def is_valid_cpf(value: str) -> bool:
    cpf = only_digits(value)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    first = _check_digit(cpf[:9], list(range(10, 1, -1)))
    second = _check_digit(cpf[:10], list(range(11, 1, -1)))
    return cpf[-2:] == f"{first}{second}"


def is_valid_cnpj(value: str) -> bool:
    cnpj = only_digits(value)
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False
    weights_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    weights_2 = [6, *weights_1]
    first = _check_digit(cnpj[:12], weights_1)
    second = _check_digit(cnpj[:13], weights_2)
    return cnpj[-2:] == f"{first}{second}"
