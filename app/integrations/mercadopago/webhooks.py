"""Validação da assinatura `x-signature` das notificações (Webhooks) do Mercado Pago.

Procedimento oficial:
1. `x-signature` tem o formato `ts=<timestamp>,v1=<hmac>`.
2. Manifesto: `id:[data.id_url];request-id:[x-request-id_header];ts:[ts_header];`
   - `data.id` vem do query param da URL; se alfanumérico, deve estar em minúsculas.
   - Segmentos cujos valores não vieram na notificação são removidos.
3. HMAC-SHA256 (hex) do manifesto com a assinatura secreta da aplicação como chave.
4. Comparar com `v1` em tempo constante; opcionalmente aplicar tolerância sobre `ts`.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SignatureResult:
    valid: bool
    reason: str | None = None
    timestamp: int | None = None


def parse_signature_header(header: str) -> tuple[str | None, str | None]:
    ts: str | None = None
    v1: str | None = None
    for part in header.split(","):
        key, sep, value = part.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        if key == "ts":
            ts = value.strip()
        elif key == "v1":
            v1 = value.strip()
    return ts, v1


def build_manifest(data_id: str | None, request_id: str | None, ts: str) -> str:
    parts = []
    if data_id:
        parts.append(f"id:{data_id.lower() if data_id.isalnum() else data_id};")
    if request_id:
        parts.append(f"request-id:{request_id};")
    parts.append(f"ts:{ts};")
    return "".join(parts)


def compute_signature(secret: str, manifest: str) -> str:
    return hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()


def verify_signature(
    *,
    secret: str,
    x_signature: str | None,
    x_request_id: str | None,
    data_id: str | None,
    tolerance_seconds: int,
    now: float | None = None,
) -> SignatureResult:
    if not x_signature:
        return SignatureResult(False, "missing_signature")
    ts, v1 = parse_signature_header(x_signature)
    if not ts or not v1 or not ts.isdigit():
        return SignatureResult(False, "malformed_signature")

    expected = compute_signature(secret, build_manifest(data_id, x_request_id, ts))
    if not hmac.compare_digest(expected, v1.lower()):
        return SignatureResult(False, "signature_mismatch")

    timestamp = int(ts)
    if tolerance_seconds > 0:
        # O provedor documenta `ts` em milissegundos; aceita também segundos.
        seconds = timestamp / 1000 if timestamp > 10**11 else float(timestamp)
        current = time.time() if now is None else now
        if abs(current - seconds) > tolerance_seconds:
            return SignatureResult(False, "timestamp_out_of_tolerance", timestamp)
    return SignatureResult(True, None, timestamp)
