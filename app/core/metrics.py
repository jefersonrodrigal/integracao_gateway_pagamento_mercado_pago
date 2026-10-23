"""Métricas Prometheus expostas em `/metrics`."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "http_requests_total", "Requisições HTTP recebidas.", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "Latência das requisições HTTP.", ["method", "route"]
)
PROVIDER_REQUESTS = Counter(
    "mercadopago_requests_total",
    "Chamadas ao Mercado Pago por operação e resultado.",
    ["operation", "outcome"],
)
PROVIDER_LATENCY = Histogram(
    "mercadopago_request_duration_seconds", "Latência das chamadas ao Mercado Pago.", ["operation"]
)
PAYMENT_ATTEMPTS = Counter(
    "payment_attempts_total", "Tentativas de pagamento iniciadas.", ["method"]
)
PAYMENT_STATUS_CHANGES = Counter(
    "payment_status_changes_total", "Transições de estado de pagamento.", ["status", "source"]
)
PAYMENT_OUTCOME_UNKNOWN = Counter(
    "payment_outcome_unknown_total", "Criações de pagamento com resultado desconhecido."
)
WEBHOOK_EVENTS = Counter(
    "webhook_events_total", "Notificações recebidas por resultado.", ["result"]
)
WEBHOOK_DEAD_LETTERS = Counter(
    "webhook_dead_letters_total", "Eventos que esgotaram as tentativas de processamento."
)
STALE_UPDATES_IGNORED = Counter(
    "stale_updates_ignored_total", "Atualizações antigas/fora de ordem descartadas.", ["entity"]
)
FINANCIAL_DIVERGENCES = Counter(
    "financial_divergences_total", "Divergências entre estado interno e provedor.", ["kind"]
)
