from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, StringConstraints, field_validator, model_validator

from app.core.validators import is_valid_cnpj, is_valid_cpf, only_digits
from app.domain.states import OrderStatus, PaymentMethod, PaymentStatus
from app.schemas.common import APIModel, Money

Email = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9\-]+)+$",
        max_length=254,
    ),
]
Name = Annotated[str, StringConstraints(min_length=1, max_length=60)]


class Identification(APIModel):
    type: Literal["CPF", "CNPJ"]
    number: Annotated[str, StringConstraints(min_length=11, max_length=18)]

    @model_validator(mode="after")
    def _validate_document(self) -> Self:
        digits = only_digits(self.number)
        valid = is_valid_cpf(digits) if self.type == "CPF" else is_valid_cnpj(digits)
        if not valid:
            raise ValueError(f"{self.type} inválido")
        self.number = digits
        return self


class Address(APIModel):
    zip_code: Annotated[str, StringConstraints(pattern=r"^\d{5}-?\d{3}$")]
    street_name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    street_number: Annotated[str, StringConstraints(min_length=1, max_length=10)] = Field(
        description='Use "N/A" quando não houver número.'
    )
    neighborhood: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    city: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    state: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
    complement: Annotated[str, StringConstraints(max_length=80)] | None = None

    @field_validator("zip_code")
    @classmethod
    def _digits(cls, value: str) -> str:
        return only_digits(value)


class PixPayer(APIModel):
    email: Email


class CardPayer(APIModel):
    email: Email
    identification: Identification | None = None


class BoletoPayer(APIModel):
    email: Email
    first_name: Name
    last_name: Name
    identification: Identification
    address: Address


class PixPaymentCreate(APIModel):
    method: Literal["pix"]
    payer: PixPayer


class CardPaymentCreate(APIModel):
    """Cartão tokenizado no front-end (MercadoPago.js / Card Payment Brick).

    O número do cartão e o CVV nunca chegam a esta API; apenas o token de uso único.
    """

    method: Literal["credit_card", "debit_card"]
    token: SecretStr = Field(min_length=8, max_length=256, description="Token de cartão do MP")
    payment_method_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{2,32}$")] = Field(
        description="Bandeira retornada por getPaymentMethods (ex.: master, visa)."
    )
    installments: int = Field(default=1, ge=1, le=24)
    capture: bool = Field(
        default=True,
        description="false reserva o valor (capture_mode=manual) para captura posterior.",
    )
    payer: CardPayer

    @model_validator(mode="after")
    def _debit_rules(self) -> Self:
        if self.method == "debit_card" and self.installments != 1:
            raise ValueError("cartão de débito aceita apenas 1 parcela")
        if self.method == "debit_card" and not self.capture:
            raise ValueError("captura manual disponível apenas para crédito")
        return self


class BoletoPaymentCreate(APIModel):
    method: Literal["boleto"]
    payer: BoletoPayer


PaymentCreate = Annotated[
    PixPaymentCreate | CardPaymentCreate | BoletoPaymentCreate, Field(discriminator="method")
]


class PixData(APIModel):
    qr_code: str | None = None
    qr_code_base64: str | None = None
    ticket_url: str | None = None
    date_of_expiration: str | None = None


class BoletoData(APIModel):
    ticket_url: str | None = None
    digitable_line: str | None = None
    barcode_content: str | None = None
    reference: str | None = None
    verification_code: str | None = None
    financial_institution: str | None = None
    date_of_expiration: str | None = None


class PaymentOut(APIModel):
    id: uuid.UUID
    order_id: uuid.UUID
    status: PaymentStatus
    method: PaymentMethod
    payment_method_id: str
    amount: Money
    currency: str
    installments: int
    capture_mode: str
    provider_order_id: str | None
    provider_payment_id: str | None
    provider_status: str | None
    provider_status_detail: str | None
    paid_amount: Money | None
    refunded_amount: Money
    outcome_unknown: bool = Field(
        description="true quando o resultado no provedor ainda está sendo reconciliado."
    )
    pix: PixData | None = None
    boleto: BoletoData | None = None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, payment: Any) -> PaymentOut:
        data = payment.payment_data or {}
        return cls(
            id=payment.id,
            order_id=payment.order_id,
            status=payment.status,
            method=payment.method,
            payment_method_id=payment.payment_method_id,
            amount=payment.amount,
            currency=payment.currency,
            installments=payment.installments,
            capture_mode=payment.capture_mode,
            provider_order_id=payment.provider_order_id,
            provider_payment_id=payment.provider_payment_id,
            provider_status=payment.provider_status,
            provider_status_detail=payment.provider_status_detail,
            paid_amount=payment.paid_amount,
            refunded_amount=payment.refunded_amount,
            outcome_unknown=payment.status == PaymentStatus.UNKNOWN,
            pix=PixData.model_validate(_pick(data, PixData))
            if payment.method == "pix" and data
            else None,
            boleto=BoletoData.model_validate(_pick(data, BoletoData))
            if payment.method == "boleto" and data
            else None,
            expires_at=payment.expires_at,
            created_at=payment.created_at,
            updated_at=payment.updated_at,
        )


def _pick(data: dict[str, Any], model: type[APIModel]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key in model.model_fields}


class PaymentStatusOut(APIModel):
    order_id: uuid.UUID
    order_status: OrderStatus
    current_payment: PaymentOut | None
    payments: list[PaymentOut]
