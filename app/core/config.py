"""Configuração da aplicação, validada na inicialização.

Segredos são lidos de variáveis de ambiente (ou `.env` em desenvolvimento) e mantidos
como `SecretStr`, de modo que nunca apareçam em `repr()`, logs ou mensagens de erro.
"""

from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]

# ISO 8601 duration aceito pela Orders API em `expiration_time` (ex.: PT30M, P3D).
_ISO_DURATION = re.compile(r"^P(?!$)(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+S)?)?$")


class ConfigurationError(RuntimeError):
    """Erro de configuração com mensagem segura (sem valores de entrada)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # Aplicação
    app_env: Environment = "development"
    app_name: str = "payment-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    docs_enabled: bool = True
    metrics_enabled: bool = True

    # Banco de dados
    database_url: SecretStr
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    db_statement_timeout_ms: int = Field(default=15_000, ge=0)

    # Mercado Pago
    mercado_pago_access_token: SecretStr
    mercado_pago_webhook_secret: SecretStr
    mercado_pago_base_url: str = "https://api.mercadopago.com"
    mercado_pago_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    mercado_pago_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    mercado_pago_max_retries: int = Field(default=2, ge=0, le=5)
    mercado_pago_retry_base_delay_seconds: float = Field(default=0.5, ge=0)
    mercado_pago_retry_max_delay_seconds: float = Field(default=8.0, ge=0)
    mercado_pago_statement_descriptor: str | None = Field(default=None, max_length=22)
    mercado_pago_payment_methods_cache_seconds: int = Field(default=600, ge=0)
    pix_expiration: str = "PT1H"
    boleto_expiration: str = "P3D"

    # Webhooks
    webhook_signature_tolerance_seconds: int = Field(default=600, ge=0)
    webhook_max_attempts: int = Field(default=10, ge=1, le=100)
    webhook_process_inline: bool = True

    # Autenticação (JWT emitido pelo provedor de identidade da plataforma)
    jwt_secret: SecretStr
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "payment-api"
    jwt_audience: str = "payment-api"
    jwt_leeway_seconds: int = Field(default=30, ge=0, le=300)

    # Segurança HTTP
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    force_https: bool = False
    trust_proxy_headers: bool = False
    rate_limit_per_minute: int = Field(default=120, ge=1)
    rate_limit_webhook_per_minute: int = Field(default=600, ge=1)
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024)

    # Regras comerciais
    currency: Literal["BRL"] = "BRL"
    shipping_flat_fee: Decimal = Field(
        default=Decimal("0.00"), ge=0, max_digits=12, decimal_places=2
    )
    free_shipping_threshold: Decimal | None = Field(
        default=None, ge=0, max_digits=12, decimal_places=2
    )
    max_order_amount: Decimal = Field(
        default=Decimal("100000.00"), gt=0, max_digits=12, decimal_places=2
    )
    max_installments: int = Field(default=12, ge=1, le=24)

    # Idempotência, reconciliação e retenção
    idempotency_ttl_hours: int = Field(default=24, ge=1, le=24 * 30)
    idempotency_lock_timeout_seconds: int = Field(default=60, ge=5)
    reconcile_interval_seconds: int = Field(default=600, ge=30)
    unknown_payment_give_up_seconds: int = Field(default=3600, ge=300)
    pending_refund_give_up_seconds: int = Field(default=86_400, ge=600)
    payment_event_retention_days: int = Field(default=365, ge=30)
    worker_poll_interval_seconds: float = Field(default=5.0, gt=0)
    worker_batch_size: int = Field(default=20, ge=1, le=500)

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("pix_expiration", "boleto_expiration")
    @classmethod
    def _validate_duration(cls, value: str) -> str:
        if not _ISO_DURATION.match(value):
            raise ValueError("deve ser uma duração ISO 8601 (ex.: PT1H, P3D)")
        return value

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("deve usar o driver assíncrono: postgresql+asyncpg://")
        return value

    @field_validator("mercado_pago_access_token", "mercado_pago_webhook_secret", "jwt_secret")
    @classmethod
    def _not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("é obrigatório e não pode ser vazio")
        return value

    @model_validator(mode="after")
    def _validate_environment(self) -> Self:
        if len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET deve ter ao menos 32 caracteres")
        if self.app_env == "production":
            if not self.mercado_pago_base_url.startswith("https://"):
                raise ValueError("MERCADO_PAGO_BASE_URL deve usar HTTPS em produção")
            if not self.force_https:
                raise ValueError("FORCE_HTTPS deve ser true em produção")
            if "*" in self.cors_allowed_origins:
                raise ValueError("CORS_ALLOWED_ORIGINS não pode conter '*' em produção")
            if self.docs_enabled:
                raise ValueError("DOCS_ENABLED deve ser false em produção")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def load_settings(**overrides: object) -> Settings:
    """Carrega e valida as configurações, convertendo erros em mensagens seguras."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in error["loc"]) or "settings"
            problems.append(f"{location.upper()}: {error['msg']}")
        raise ConfigurationError(
            "Configuração inválida ou incompleta: " + "; ".join(problems)
        ) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
