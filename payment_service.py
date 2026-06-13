"""付款服務 — 支援 mock 與 Stripe Checkout。"""
import os
import json
import time


def payment_provider() -> str:
    return os.environ.get("PAYMENT_PROVIDER", "mock")


def stripe_enabled() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def get_local_ip() -> str:
    """取得本機在區域網路中的 IP 位址。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP


def public_origin(req) -> str:
    configured = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if configured:
        local_ip = get_local_ip()
        if "localhost" in configured:
            configured = configured.replace("localhost", local_ip)
        elif "127.0.0.1" in configured:
            configured = configured.replace("127.0.0.1", local_ip)
        return configured
    proto = req.headers.get("X-Forwarded-Proto", "http")
    host = req.headers.get("X-Forwarded-Host") or req.host
    if "localhost" in host:
        host = host.replace("localhost", get_local_ip())
    elif "127.0.0.1" in host:
        host = host.replace("127.0.0.1", get_local_ip())
    return f"{proto}://{host}"


def create_stripe_checkout_session(req, order: dict) -> dict:
    """建立 Stripe Checkout Session，回傳 session dict（含 id 與 url）。"""
    import stripe
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise RuntimeError("Stripe 尚未設定")
    stripe.api_key = key

    origin = public_origin(req)
    currency = os.environ.get("PAYMENT_CURRENCY", "twd").lower()

    session = stripe.checkout.Session.create(
        mode="payment",
        client_reference_id=str(order["id"]),
        success_url=f"{origin}/payment/{order['id']}?session_id={{CHECKOUT_SESSION_ID}}&success=1",
        cancel_url=f"{origin}/payment/{order['id']}?canceled=1",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": currency,
                "product_data": {
                    "name": f"餐點訂單 #{order['id']}",
                    "description": f"桌號 {order['table_name']}",
                },
                "unit_amount": order["total"],
            },
        }],
        metadata={
            "orderId": str(order["id"]),
            "table": order["table_slug"],
        },
    )
    return {"id": session.id, "url": session.url}
