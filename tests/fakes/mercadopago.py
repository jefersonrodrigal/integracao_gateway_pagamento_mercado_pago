"""Simulador em memória da Orders API do Mercado Pago (somente testes).

Reproduz os contratos documentados usados pela integração: criação com X-Idempotency-Key,
consulta, busca por external_reference, cancelamento, captura, reembolso e payment_methods.
O comportamento de cartão é escolhido pelo token: "approved-*", "rejected-*", "review-*".
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

_counter = itertools.count(1)


def _id(prefix: str) -> str:
    return f"{prefix}01TEST{next(_counter):020d}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakeMercadoPago:
    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, str] = {}  # X-Idempotency-Key -> order id
        self.used_write_keys: set[str] = set()
        self.requests: list[httpx.Request] = []
        # Falhas injetáveis, consumidas na ordem: "timeout_after_commit", "connect_error",
        # "http_500", "http_429", "http_401", "invalid_properties".
        self.create_failures: list[str] = []
        self.get_failures: list[str] = []
        self.refund_failures: list[str] = []
        self.payment_methods: list[dict[str, Any]] = [
            {"id": "pix", "name": "PIX", "payment_type_id": "bank_transfer", "status": "active"},
            {
                "id": "bolbradesco",
                "name": "Boleto",
                "payment_type_id": "ticket",
                "status": "active",
            },
            {
                "id": "master",
                "name": "Mastercard",
                "payment_type_id": "credit_card",
                "status": "active",
                "secure_thumbnail": "https://example.test/master.png",
            },
            {"id": "visa", "name": "Visa", "payment_type_id": "credit_card", "status": "active"},
            {
                "id": "debvisa",
                "name": "Visa Débito",
                "payment_type_id": "debit_card",
                "status": "inactive",
            },
        ]

    # ------------------------------------------------------------------ helpers
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def calls(self, method: str, path_prefix: str) -> list[httpx.Request]:
        return [
            r for r in self.requests if r.method == method and r.url.path.startswith(path_prefix)
        ]

    def set_status(self, order_id: str, status: str, detail: str, *, touch: bool = True) -> None:
        order = self.orders[order_id]
        order["status"], order["status_detail"] = status, detail
        payment = order["transactions"]["payments"][0]
        payment["status"], payment["status_detail"] = status, detail
        if status == "processed" and detail == "accredited":
            payment["paid_amount"] = payment["amount"]
            order["total_paid_amount"] = order["total_amount"]
        if touch:
            order["last_updated_date"] = (
                datetime.fromisoformat(order["last_updated_date"]) + timedelta(seconds=1)
            ).isoformat()

    def approve(self, order_id: str) -> None:
        self.set_status(order_id, "processed", "accredited")

    def only_order(self) -> dict[str, Any]:
        assert len(self.orders) == 1, self.orders.keys()
        return next(iter(self.orders.values()))

    # ------------------------------------------------------------------ handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        assert request.headers["authorization"].startswith("Bearer ")
        if request.method == "GET" and path == "/v1/payment_methods":
            return httpx.Response(200, json=self.payment_methods)
        if request.method == "POST" and path == "/v1/orders":
            return self._create(request)
        if request.method == "GET" and path == "/v1/orders":
            return self._search(request)
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["v1", "orders"]:
            order = self.orders.get(parts[2])
            if request.method == "GET" and len(parts) == 3:
                if self.get_failures:
                    return self._failure(self.get_failures.pop(0), request)
                if order is None:
                    return _error(404, "order_not_found")
                return httpx.Response(200, json=order, headers={"x-request-id": "req-get"})
            if request.method == "POST" and len(parts) == 4:
                if not request.headers.get("x-idempotency-key"):
                    return _error(400, "empty_required_header")
                if order is None:
                    return _error(404, "order_not_found")
                action = parts[3]
                if action == "cancel":
                    return self._cancel(order)
                if action == "capture":
                    return self._capture(order)
                if action == "refund":
                    return self._refund(order, request)
        return _error(404, "not_found")

    def _failure(self, kind: str, request: httpx.Request) -> httpx.Response:
        if kind == "connect_error":
            raise httpx.ConnectError("connection refused", request=request)
        if kind == "read_timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        status = int(kind.removeprefix("http_"))
        return _error(
            status,
            {500: "internal_error", 429: "too_many_requests", 401: "unauthorized"}.get(
                status, "error"
            ),
        )

    def _create(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-idempotency-key")
        if not key:
            return _error(400, "empty_required_header")
        body = json.loads(request.content)
        if key in self.keys:
            return _error(409, "idempotency_key_already_used")

        failure = self.create_failures.pop(0) if self.create_failures else None
        if failure in {"connect_error", "http_500", "http_429", "http_401"}:
            return self._failure(failure, request)
        if failure == "invalid_properties":
            return _error(400, "invalid_properties")

        payment_in = body["transactions"]["payments"][0]
        assert Decimal(body["total_amount"]) == Decimal(payment_in["amount"])
        method = payment_in["payment_method"]
        order_id, payment_id = _id("ORD"), _id("PAY")
        payment: dict[str, Any] = {
            "id": payment_id,
            "amount": payment_in["amount"],
            "reference_id": str(next(_counter)),
            "payment_method": {k: v for k, v in method.items() if k != "token"},
        }
        order: dict[str, Any] = {
            "id": order_id,
            "type": "online",
            "processing_mode": body["processing_mode"],
            "external_reference": body["external_reference"],
            "total_amount": body["total_amount"],
            "capture_mode": body.get("capture_mode", "automatic"),
            "country_code": "BRA",
            "created_date": _now(),
            "last_updated_date": _now(),
            "transactions": {"payments": [payment]},
        }
        status_code = 201
        if method["type"] == "bank_transfer":
            payment["payment_method"].update(
                qr_code="00020126580014br.gov.bcb.pix0136test",
                qr_code_base64="iVBORw0KGgo=",
                ticket_url="https://www.mercadopago.com.br/sandbox/payments/1/ticket",
            )
            status, detail = "action_required", "waiting_transfer"
        elif method["type"] == "ticket":
            payment["payment_method"].update(
                digitable_line="23790001246000000000000000000000000000000000000",
                barcode_content="23791000000000000000000000000000000000000000",
                ticket_url="https://www.mercadopago.com.br/sandbox/payments/1/ticket",
            )
            status, detail = "action_required", "waiting_payment"
        else:
            token = method["token"]
            if token.startswith("rejected"):
                status, detail, status_code = "failed", "rejected_by_issuer", 402
            elif token.startswith("review"):
                status, detail = "processing", "in_review"
            elif order["capture_mode"] == "manual":
                status, detail = "action_required", "waiting_capture"
            else:
                status, detail = "processed", "accredited"
        order["status"], order["status_detail"] = status, detail
        payment["status"], payment["status_detail"] = status, detail
        if status == "processed":
            payment["paid_amount"] = payment["amount"]
            order["total_paid_amount"] = order["total_amount"]
        self.orders[order_id] = order
        self.keys[key] = order_id

        if failure == "timeout_after_commit":
            raise httpx.ReadTimeout("timeout after commit", request=request)
        if status_code == 402:
            return httpx.Response(
                402, json={**order, "errors": [{"code": "failed", "message": "x"}]}
            )
        return httpx.Response(201, json=order, headers={"x-request-id": "req-create"})

    def _search(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "begin_date" not in params or "end_date" not in params:
            return _error(400, "missing_required_params")
        ref = params.get("external_reference")
        data = [o for o in self.orders.values() if o["external_reference"] == ref]
        return httpx.Response(200, json={"data": data, "paging": {"total": len(data)}})

    def _cancel(self, order: dict[str, Any]) -> httpx.Response:
        if order["status"] == "canceled":
            return _error(409, "order_already_canceled")
        if order["status"] not in {"action_required", "created"}:
            return _error(409, "cannot_cancel_order")
        self.set_status(order["id"], "canceled", "canceled")
        order["status_detail"] = "canceled_transaction"
        return httpx.Response(200, json=order)

    def _capture(self, order: dict[str, Any]) -> httpx.Response:
        if order["status_detail"] != "waiting_capture":
            return _error(409, "cannot_capture_order")
        self.approve(order["id"])
        return httpx.Response(200, json=order)

    def _refund(self, order: dict[str, Any], request: httpx.Request) -> httpx.Response:
        key = request.headers["x-idempotency-key"]
        if key in self.used_write_keys:
            return _error(409, "idempotency_key_already_used")
        if self.refund_failures:
            failure = self.refund_failures[0]
            if failure == "read_timeout":
                self.used_write_keys.add(key)
        else:
            self.used_write_keys.add(key)
        if self.refund_failures:
            failure = self.refund_failures.pop(0)
            if failure == "read_timeout":
                self._apply_refund(order, request)
            return self._failure(failure, request)
        if order["status"] not in {"processed"} or order["status_detail"] == "refunded":
            return _error(409, "cannot_refund_order")
        return self._apply_refund(order, request) or _error(400, "refund_amount_exceeds")

    def _apply_refund(self, order: dict[str, Any], request: httpx.Request) -> httpx.Response | None:
        payment = order["transactions"]["payments"][0]
        refunds = order["transactions"].setdefault("refunds", [])
        already = sum(Decimal(r["amount"]) for r in refunds)
        total = Decimal(payment["amount"])
        body = json.loads(request.content) if request.content else None
        amount = Decimal(body["transactions"][0]["amount"]) if body else total - already
        if amount <= 0 or already + amount > total:
            return None
        refunds.append(
            {
                "id": _id("REF"),
                "transaction_id": payment["id"],
                "amount": f"{amount:.2f}",
                "status": "processed",
            }
        )
        if already + amount == total:
            self.set_status(order["id"], "refunded", "refunded")
        else:
            self.set_status(order["id"], "processed", "partially_refunded")
        return httpx.Response(201, json=order)


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"errors": [{"code": code, "message": code}]})
