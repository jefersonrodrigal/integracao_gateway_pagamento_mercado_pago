"""Mapeamento entre estados da Orders API e estados internos.

Estados oficiais de transação (status/status_detail) — documentação "Status de transação":
    created/created                       -> pending   (existe no provedor, ainda não processada)
    processing/in_process                 -> pending
    processing/{in_review,pending_review_manual}, in_review/* -> in_review
    action_required/waiting_capture       -> authorized (reservado; NÃO é captura)
    action_required/{waiting_payment, waiting_transfer, pending_challenge, waiting_retry} -> pending
    processed/accredited                  -> approved
    processed/partially_refunded          -> partially_refunded
    refunded/* ou */refunded              -> refunded
    failed/expired, canceled/expired, expired/* -> expired
    failed/*                              -> rejected
    canceled/*                            -> cancelled
    charged_back/*                        -> charged_back (requer revisão manual)

Qualquer combinação não documentada retorna None: o estado interno é preservado e uma
divergência é registrada, em vez de presumir um significado.
"""

from __future__ import annotations

from typing import Any

from app.domain.states import PaymentStatus, RefundStatus
from app.integrations.mercadopago.schemas import MPOrder, MPPayment

_REVIEW_DETAILS = frozenset({"in_review", "pending_review_manual"})
_PENDING_ACTION_DETAILS = frozenset(
    {"waiting_payment", "waiting_transfer", "pending_challenge", "waiting_retry"}
)


def map_payment_status(status: str | None, detail: str | None) -> PaymentStatus | None:
    if not status:
        return None
    status = status.lower()
    detail = (detail or "").lower()

    if status == "refunded" or detail == "refunded":
        return PaymentStatus.REFUNDED
    if detail == "partially_refunded":
        return PaymentStatus.PARTIALLY_REFUNDED
    if status == "charged_back":
        return PaymentStatus.CHARGED_BACK
    if status == "expired" or (status in {"failed", "canceled"} and detail == "expired"):
        return PaymentStatus.EXPIRED
    match status:
        case "processed":
            return PaymentStatus.APPROVED if detail in {"accredited", ""} else None
        case "action_required":
            if detail == "waiting_capture":
                return PaymentStatus.AUTHORIZED
            return (
                PaymentStatus.PENDING if detail in _PENDING_ACTION_DETAILS or not detail else None
            )
        case "processing":
            return PaymentStatus.IN_REVIEW if detail in _REVIEW_DETAILS else PaymentStatus.PENDING
        case "in_review":
            return PaymentStatus.IN_REVIEW
        case "created":
            return PaymentStatus.PENDING
        case "failed":
            return PaymentStatus.REJECTED
        case "canceled" | "cancelled":
            return PaymentStatus.CANCELLED
    return None


def map_order_payment_status(order: MPOrder) -> tuple[PaymentStatus | None, str | None, str | None]:
    """Estado interno a partir da transação (preferida) ou, na falta dela, da order."""
    payment = order.payment
    status = payment.status if payment and payment.status else order.status
    detail = payment.status_detail if payment and payment.status else order.status_detail
    return map_payment_status(status, detail), status, detail


def map_refund_status(status: str | None) -> RefundStatus:
    if status and status.lower() in {"processed", "refunded", "approved"}:
        return RefundStatus.APPROVED
    if status and status.lower() in {"failed", "rejected", "canceled", "cancelled"}:
        return RefundStatus.REJECTED
    return RefundStatus.PENDING


_PUBLIC_PAYMENT_FIELDS = (
    "ticket_url",
    "qr_code",
    "qr_code_base64",
    "barcode_content",
    "digitable_line",
    "reference",
    "verification_code",
    "financial_institution",
)


def extract_payment_data(payment: MPPayment | None) -> dict[str, Any] | None:
    """Dados exibíveis ao pagador (Pix/boleto). Nunca inclui token ou dados de cartão."""
    if payment is None:
        return None
    method = payment.payment_method
    data = {name: getattr(method, name) for name in _PUBLIC_PAYMENT_FIELDS}
    data = {key: value for key, value in data.items() if value}
    if payment.date_of_expiration:
        data["date_of_expiration"] = payment.date_of_expiration.isoformat()
    return data or None
