"""Logging estruturado (JSON) com identificador de correlação e redação de dados sensíveis."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

# Chaves cujo valor nunca deve ser registrado (comparação sem diferenciar maiúsculas).
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "access_token",
        "token",
        "secret",
        "password",
        "x-signature",
        "card_number",
        "security_code",
        "cvv",
        "identification",
        "number",
        "email",
        "first_name",
        "last_name",
        "address",
        "phone",
        "qr_code",
        "qr_code_base64",
        "barcode_content",
        "digitable_line",
        "database_url",
    }
)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)
_MP_TOKEN = re.compile(r"\b(APP_USR|TEST)-[A-Za-z0-9\-]{10,}\b")
REDACTED = "***"


def redact(value: Any, _depth: int = 0) -> Any:
    """Remove recursivamente valores sensíveis de estruturas antes de registrar."""
    if _depth > 8:
        return REDACTED
    if isinstance(value, dict):
        return {
            key: REDACTED
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS
            else redact(item, _depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return _MP_TOKEN.sub(REDACTED, _BEARER.sub(rf"\1{REDACTED}", value))
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, app_name: str, environment: str) -> None:
        super().__init__()
        self._app_name = app_name
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "app": self._app_name,
            "env": self._environment,
        }
        request_id = request_id_ctx.get()
        if request_id:
            payload["request_id"] = request_id
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(redact(fields))
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = getattr(record, "fields", None)
        request_id = request_id_ctx.get()
        extra = f" request_id={request_id}" if request_id else ""
        if isinstance(fields, dict) and fields:
            extra += " " + json.dumps(redact(fields), default=str, ensure_ascii=False)
        return str(redact(base + extra))


def configure_logging(level: str, *, json_output: bool, app_name: str, environment: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter(app_name, environment))
    else:
        handler.setFormatter(TextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # O httpx registra URLs e cabeçalhos em DEBUG; mantém silencioso.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log_fields(**fields: Any) -> dict[str, Any]:
    """Atalho para `logger.info("msg", extra=log_fields(chave=valor))`."""
    return {"fields": fields}
