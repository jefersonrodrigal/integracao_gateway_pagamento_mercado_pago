from datetime import timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core.config import ConfigurationError, load_settings
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.logging import redact
from app.core.middleware import SlidingWindowRateLimiter
from app.core.security import create_access_token, decode_access_token
from app.core.validators import is_valid_cnpj, is_valid_cpf
from app.schemas.orders import OrderCreate
from app.schemas.payments import PaymentCreate
from tests.conftest import BOLETO, card, make_settings

ADAPTER: TypeAdapter[PaymentCreate] = TypeAdapter(PaymentCreate)


def test_cpf_cnpj_validation() -> None:
    assert is_valid_cpf("191.191.191-00")
    assert not is_valid_cpf("111.111.111-11")
    assert not is_valid_cpf("123")
    assert is_valid_cnpj("11.222.333/0001-81")
    assert not is_valid_cnpj("11.222.333/0001-80")


def test_payment_schema_discriminates_by_method() -> None:
    assert ADAPTER.validate_python(card()).method == "credit_card"
    boleto = ADAPTER.validate_python(BOLETO)
    assert boleto.payer.identification.number == "19119119100"  # type: ignore[union-attr]
    assert boleto.payer.address.zip_code == "06233903"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "payload",
    [
        card(method="debit_card", installments=3),
        card(installments=0),
        card(card_number="5031433215406351"),  # PAN nunca é aceito (extra=forbid)
        {
            **BOLETO,
            "payer": {
                **BOLETO["payer"],
                "identification": {"type": "CPF", "number": "12345678900"},
            },
        },
        {"method": "pix", "payer": {"email": "invalido"}},
        {"method": "crypto"},
    ],
)
def test_invalid_payment_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(payload)


def test_card_token_is_secret_in_repr() -> None:
    parsed = ADAPTER.validate_python(card("approved-super-secret"))
    assert "approved-super-secret" not in repr(parsed)


def test_order_schema_rejects_duplicates_and_prices() -> None:
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(
            {"items": [{"sku": "A", "quantity": 1}, {"sku": "A", "quantity": 2}]}
        )
    with pytest.raises(ValidationError):
        OrderCreate.model_validate({"items": [{"sku": "A", "quantity": 1, "unit_price": "0.01"}]})


def test_settings_errors_do_not_leak_values() -> None:
    with pytest.raises(ConfigurationError) as exc:
        load_settings(
            _env_file=None,
            database_url="postgresql://user:SuperSecret@db/x",
            mercado_pago_access_token="APP_USR-leak",
            mercado_pago_webhook_secret="s",
            jwt_secret="short",
        )
    message = str(exc.value)
    assert "DATABASE_URL" in message
    assert "SuperSecret" not in message
    assert "APP_USR-leak" not in message


def test_production_requires_hardening() -> None:
    with pytest.raises(ConfigurationError, match="FORCE_HTTPS"):
        make_settings(app_env="production", docs_enabled=False)


def test_jwt_roundtrip_and_rejections() -> None:
    settings = make_settings()
    principal = decode_access_token(settings, create_access_token(settings, "c1", ["customer"]))
    assert principal.subject == "c1" and not principal.is_admin
    expired = create_access_token(settings, "c1", expires_in=timedelta(minutes=-10))
    with pytest.raises(AuthenticationError):
        decode_access_token(settings, expired)
    other = make_settings(jwt_secret="another-secret-with-at-least-32-chars!!")
    with pytest.raises(AuthenticationError):
        decode_access_token(other, create_access_token(settings, "c1"))
    with pytest.raises(PermissionDeniedError):
        decode_access_token(settings, create_access_token(settings, "c1", ["superuser"]))


def test_redaction_of_sensitive_fields() -> None:
    data = {
        "authorization": "Bearer abc",
        "payer": {"email": "a@b.com", "identification": {"number": "1"}},
        "message": "token APP_USR-1234567890-abcdef used",
        "amount": "10.00",
    }
    redacted = redact(data)
    assert redacted["authorization"] == "***"
    assert redacted["payer"]["email"] == "***"
    assert "APP_USR" not in redacted["message"]
    assert redacted["amount"] == "10.00"


def test_rate_limiter_sliding_window() -> None:
    now = [0.0]
    limiter = SlidingWindowRateLimiter(clock=lambda: now[0])
    assert limiter.hit("k", 2) is None
    assert limiter.hit("k", 2) is None
    assert limiter.hit("k", 2) == pytest.approx(60.0)
    now[0] = 61.0
    assert limiter.hit("k", 2) is None


def test_metrics_route_label_uses_template_not_ids() -> None:
    from types import SimpleNamespace

    from app.core.middleware import _route_template

    scope = {
        "path": "/api/v1/orders/3f1c/payment-status",
        "route": SimpleNamespace(path="/orders/{order_id}/payment-status"),
    }
    assert _route_template(scope) == "/api/v1/orders/{order_id}/payment-status"
    assert _route_template({"path": "/x"}) == "unmatched"
