"""
QR Code 點餐系統 — Python / Flask 版
"""
import os
import io
import json
import time
import queue
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# pyrefly: ignore [missing-import]
from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, make_response, Response, abort, send_file
)
import qrcode
import qrcode.image.svg

import db
import events
from auth import admin_token, is_admin_request, require_admin, require_admin_api
from payment_service import (
    payment_provider, stripe_enabled, public_origin,
    create_stripe_checkout_session
)

# ---------------------------------------------------------------------------
# 應用程式初始化
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("ADMIN_SECRET", "change-this-secret")
app.json.ensure_ascii = False

# 【重要修改】將原本的 db.init_db() 註解掉
# 雲端部署至 Vercel 時，不要在啟動時自動嘗試對唯讀環境或已存在的雲端資料庫執行初始化指令
# db.init_db()


# ---------------------------------------------------------------------------
# Jinja2 全域工具
# ---------------------------------------------------------------------------

@app.template_global()
def money(value):
    return db.money(value)


@app.template_global()
def order_seq(order_number):
    """從 'YYYYMMDD0001' 提取流水號 '0001'。"""
    try:
        return order_number[-4:]
    except (TypeError, AttributeError):
        return order_number


@app.template_global()
def payment_status_label(value):
    return {"unpaid": "未付款", "paid": "已付款", "pending": "付款中", "failed": "付款失敗"}.get(value, value)


@app.template_global()
def order_status_label(value):
    return {"pending": "待製作", "preparing": "製作中", "ready": "可出餐", "completed": "已完成", "cancelled": "已取消"}.get(value, value)


@app.context_processor
def inject_globals():
    path = request.path
    is_customer = path.startswith("/t/") or path.startswith("/order/") or path.startswith("/payment/")
    return {
        "is_admin": is_admin_request(),
        "is_customer": is_customer,
        "current_year": datetime.now().year,
    }


# ---------------------------------------------------------------------------
# 前台 — 首頁
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    conn = db.get_db()
    tables = db.get_tables(conn)
    stats = db.get_dashboard_stats(conn)
    conn.close()
    return render_template("index.html", tables=tables, stats=stats)


# ---------------------------------------------------------------------------
# 前台 — 桌位點餐頁
# ---------------------------------------------------------------------------

@app.route("/t/<slug>")
def table_page(slug):
    conn = db.get_db()
    table = db.get_table_by_slug(conn, slug)
    if not table or not table["is_active"]:
        conn.close()
        abort(404)
    menu_groups = db.grouped_menu_items(conn)
    conn.close()
    # 所有可用品項的扁平列表，供前端 JS 使用
    all_items = [item for g in menu_groups for item in g["menu_list"]]
    total_items = len(all_items)
    return render_template(
        "order/table.html",
        table=table,
        menu_groups=menu_groups,
        menu_items_json=json.dumps(all_items, ensure_ascii=False),
        total_items=total_items,
    )


# ---------------------------------------------------------------------------
# 前台 — 訂單確認頁
# ---------------------------------------------------------------------------

@app.route("/order/<int:order_id>")
def order_page(order_id):
    conn = db.get_db()
    order = db.get_order_by_id(conn, order_id)
    if not order:
        conn.close()
        abort(404)
    items = db.get_order_items(conn, order_id)
    payment = db.get_payment_by_order_id(conn, order_id)
    conn.close()
    return render_template("order/detail.html", order=order, items=items, payment=payment)


# ---------------------------------------------------------------------------
# 前台 — 付款頁
# ---------------------------------------------------------------------------

@app.route("/payment/<int:order_id>")
def payment_page(order_id):
    conn = db.get_db()
    order = db.get_order_by_id(conn, order_id)
    if not order:
        conn.close()
        abort(404)
    items = db.get_order_items(conn, order_id)
    payment = db.get_payment_by_order_id(conn, order_id)
    conn.close()
    return render_template(
        "order/payment.html",
        order=order,
        items=items,
        payment=payment,
        payment_provider=payment_provider(),
        stripe_enabled=stripe_enabled(),
    )


# ---------------------------------------------------------------------------
# API — 建立訂單
# ---------------------------------------------------------------------------

@app.route("/api/orders", methods=["POST"])
def api_create_order():
    data = request.get_json(force=True, silent=True) or {}
    conn = db.get_db()
    try:
        table_slug = str(data.get("table_slug", "")).strip()
        table = db.get_table_by_slug(conn, table_slug) if table_slug else None
        if not table or not table["is_active"]:
            return jsonify(ok=False, message="桌號無效或已停用"), 400

        raw_items = data.get("items", [])
        items = []
        for it in raw_items:
            try:
                mid = int(it["menu_item_id"])
                qty = int(it["quantity"])
            except (KeyError, TypeError, ValueError):
                continue
            if mid > 0 and qty > 0:
                items.append({"menu_item_id": mid, "quantity": qty})

        with conn:
            result = db.create_order(
                conn,
                table_id=table["id"],
                customer_name=str(data.get("customer_name", "")).strip(),
                note=str(data.get("note", "")).strip(),
                items=items,
            )
            order_id = result["order_id"]
            order_number = result["order_number"]

        events.emit("order.created", {"orderId": order_id, "orderNumber": order_number})
        return jsonify(ok=True, orderId=order_id, orderNumber=order_number, redirect=f"/order/{order_id}")
    except ValueError as e:
        return jsonify(ok=False, message=str(e)), 400
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API — 付款建立
# ---------------------------------------------------------------------------

@app.route("/api/payments/create", methods=["POST"])
def api_payment_create():
    data = request.get_json(force=True, silent=True) or {}
    conn = db.get_db()
    try:
        order = db.get_order_by_id(conn, int(data.get("order_id", 0)))
        if not order:
            return jsonify(ok=False, message="找不到訂單"), 404

        provider = str(data.get("provider", payment_provider())).lower()

        if provider == "stripe":
            session = create_stripe_checkout_session(request, order)
            with conn:
                db.upsert_payment(
                    conn, order["id"], "stripe", "pending",
                    order["total"],
                    os.environ.get("PAYMENT_CURRENCY", "twd").upper(),
                    session["id"], session["url"], json.dumps(session),
                )
                db.update_order_payment_status(conn, order["id"], "pending",
                                               provider="stripe", reference=session["id"])
            events.emit("payment.created", {"orderId": order["id"], "provider": "stripe"})
            return jsonify(ok=True, redirectUrl=session["url"])

        # mock 付款
        reference = f"mock-{order['id']}-{int(time.time())}"
        with conn:
            payment = db.upsert_payment(
                conn, order["id"], "mock", "paid",
                order["total"], "TWD",
                reference, f"/payment/{order['id']}?mock=1",
                json.dumps({"mode": "mock"}),
            )
            db.mark_payment_paid(
                conn, order["id"],
                provider="mock",
                reference=reference,
                paid_at=datetime.now(timezone.utc).isoformat(),
                raw_payload=json.dumps({"mode": "mock", "paid": True}),
            )
        # 【補齊被截斷的程式碼】
        events.emit("payment.updated", {"orderId": order["id"], "status": "paid"})
        return jsonify(ok=True, redirectUrl=f"/payment/{order['id']}?mock=1")
    except Exception as e:
        return jsonify(ok=False, message=str(e)), 500
    finally:
        conn.close()
