from fastapi import APIRouter

from app.api.v1.endpoints import orders, payment_methods, payments, refunds, webhooks

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(orders.router)
api_router.include_router(payments.router)
api_router.include_router(payment_methods.router)
api_router.include_router(refunds.router)
api_router.include_router(webhooks.router)
