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


print("DB FILE =", db.__file__)

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
#    print("STATS =", stats)
#    print("TYPE =", type(stats))
#    print("ITEMS =", stats['items'])
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
        menu_items_json=json.dumps(
            all_items,
            ensure_ascii=False,
            default=str
        ),
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
        events.emit("payment.paid", {"orderId": order["id"], "provider": "mock"})
        return jsonify(ok=True, redirectUrl=f"/order/{order['id']}")

    except Exception as e:
        return jsonify(ok=False, message=str(e)), 400
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API — Stripe Webhook
# ---------------------------------------------------------------------------

@app.route("/api/payments/stripe/webhook", methods=["POST"])
def api_stripe_webhook():
    import stripe
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not secret or not key:
        return jsonify(ok=False, message="Stripe 未設定"), 500

    stripe.api_key = key
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(request.get_data(), sig, secret)
    except Exception as e:
        return jsonify(ok=False, message=str(e)), 400

    conn = db.get_db()
    try:
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            order_id = int(session.get("metadata", {}).get("orderId") or
                           session.get("client_reference_id") or 0)
            order = db.get_order_by_id(conn, order_id)
            if order:
                with conn:
                    db.mark_payment_paid(conn, order["id"],
                                         provider="stripe",
                                         reference=session["id"],
                                         paid_at=datetime.now(timezone.utc).isoformat(),
                                         raw_payload=json.dumps(session))
                events.emit("payment.paid", {"orderId": order["id"], "provider": "stripe"})

        elif event["type"] in ("checkout.session.expired", "payment_intent.payment_failed"):
            session = event["data"]["object"]
            order_id = int(session.get("metadata", {}).get("orderId") or
                           session.get("client_reference_id") or 0)
            order = db.get_order_by_id(conn, order_id)
            if order:
                with conn:
                    db.mark_payment_failed(conn, order["id"],
                                           provider="stripe",
                                           reference=session.get("id", ""),
                                           raw_payload=json.dumps(session))
                events.emit("payment.failed", {"orderId": order["id"], "provider": "stripe"})
    finally:
        conn.close()

    return jsonify(received=True)
# ---------------------------------------------------------------------------
# API — SSE 事件串流（廚房看板用）
# ---------------------------------------------------------------------------

@app.route("/api/events")
def api_events():
    if not is_admin_request():
        return Response("Unauthorized", status=401)

    def stream():
        q = events.subscribe()
        start_time = time.time()
        # Vercel 雲端環境限制單次請求時間，故設定最多運行 10 秒後自動安全斷開並由前端自動重連
        max_duration = 10 if os.environ.get("VERCEL") else float("inf")
        
        try:
            yield f"event: ready\ndata: {json.dumps({'ok': True, 'version': events.current_version()}, ensure_ascii=False)}\n\n"
            while time.time() - start_time < max_duration:
                try:
                    event = q.get(timeout=2)
                    yield f"id: {event['id']}\nevent: update\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield f"event: ping\ndata: {int(time.time())}\n\n"
        except GeneratorExit:
            pass
        finally:
            events.unsubscribe(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# API — 廚房訂單列表
# ---------------------------------------------------------------------------

@app.route("/api/kitchen/orders")
@require_admin_api
def api_kitchen_orders():
    conn = db.get_db()
    orders = db.list_kitchen_orders(conn)
    conn.close()
    return jsonify(ok=True, orders=orders)


# ---------------------------------------------------------------------------
# 廚房看板頁面
# ---------------------------------------------------------------------------

@app.route("/kitchen")
@require_admin
def kitchen():
    conn = db.get_db()
    orders = db.list_kitchen_orders(conn)
    conn.close()
    return render_template("kitchen.html", orders=orders)


# ---------------------------------------------------------------------------
# 後台 — 登入 / 登出
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if is_admin_request():
        return redirect(url_for("admin_index"))

    error = ""
    next_path = request.args.get("next", "/admin")

    if request.method == "POST":
        password = request.form.get("password", "")
        next_path = request.form.get("next", "/admin")
        safe_next = next_path if next_path.startswith("/") else "/admin"
        if password == os.environ.get("ADMIN_PASSWORD", "admin123"):
            resp = make_response(redirect(safe_next))
            resp.set_cookie("admin_auth", admin_token(), httponly=True, samesite="Lax")
            return resp
        error = "密碼錯誤"

    return render_template("admin/login.html", error=error, next_path=next_path)

@app.route("/api/admin/logout")
def admin_logout():
    resp = make_response(redirect("/"))
    resp.delete_cookie("admin_auth")
    return resp


# ---------------------------------------------------------------------------
# 後台 — 總覽
# ---------------------------------------------------------------------------

@app.route("/admin")
@require_admin
def admin_index():
    conn = db.get_db()
    stats = db.get_dashboard_stats(conn)
#    print("STATS =", stats)
#    print("TYPE =", type(stats))
#    print("ITEMS =", stats['items'])
    orders = db.list_orders(conn)[:8]
    conn.close()
    return render_template("admin/index.html", stats=stats, orders=orders)


# ---------------------------------------------------------------------------
# 後台 — 菜單管理
# ---------------------------------------------------------------------------

@app.route("/admin/menu", methods=["GET"])
@require_admin
def admin_menu():
    conn = db.get_db()
    categories = db.get_categories(conn)
    menu_groups = db.grouped_menu_items(conn)
    edit_id = request.args.get("edit", type=int)
    edit_item = None
    if edit_id:
        for g in menu_groups:
            for it in g["menu_list"]:
                if it["id"] == edit_id:
                    edit_item = it
                    break
    notice = request.args.get("notice", "")
    conn.close()
    return render_template("admin/menu.html",
                           categories=categories, menu_groups=menu_groups,
                           edit_item=edit_item, notice=notice)


# ---------------------------------------------------------------------------
# API — 圖片上傳 (已針對 Vercel 唯讀環境進行防爆重構)
# ---------------------------------------------------------------------------

@app.route("/api/admin/upload", methods=["POST"])
@require_admin_api
def api_admin_upload():
    if "file" not in request.files:
        return jsonify(ok=False, message="沒有上傳檔案"), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify(ok=False, message="未選擇檔案"), 400
    
    allowed_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in allowed_extensions:
        return jsonify(ok=False, message="不支援的檔案格式，請上傳圖片 (png, jpg, jpeg, gif, webp)"), 400

    try:
        # Vercel 不支援本地寫入硬碟，故將圖片讀入記憶體並轉為 Base64 Data URL 格式直接存入資料庫
        import base64
        file_bytes = file.read()
        if not file_bytes:
            return jsonify(ok=False, message="檔案內容為空"), 400
            
        base64_data = base64.b64encode(file_bytes).decode("utf-8")
        mime_type = f"image/{file_ext[1:]}" if file_ext != ".jpg" else "image/jpeg"
        data_url = f"data:{mime_type};base64,{base64_data}"
        
        # 直接回傳可以在前端 <img> 標籤內顯示的 Data URL 網址
        return jsonify(ok=True, url=data_url)
    except Exception as e:
        return jsonify(ok=False, message=f"圖片處理失敗: {str(e)}"), 500


@app.route("/api/admin/menu", methods=["POST"])
@require_admin_api
def api_admin_menu_upsert():
    data = request.get_json(force=True, silent=True) or request.form.to_dict()
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify(ok=False, message="商品名稱必填"), 400
    try:
        price = int(data.get("price", 0))
    except (TypeError, ValueError):
        return jsonify(ok=False, message="價格格式錯誤"), 400
    if price < 0:
        return jsonify(ok=False, message="價格格式錯誤"), 400

    payload = {
        "id": int(data["id"]) if data.get("id") else None,
        "category_id": int(data["category_id"]) if data.get("category_id") else None,
        "name": name,
        "description": str(data.get("description", "")).strip(),
        "price": price,
        "image_url": str(data.get("image_url", "")).strip(),
        # 修正 PostgreSQL 布林值相容性：1/0 轉換為 True/False
        "is_available": data.get("is_available") in ("on", "1", True, 1, "true"),
        "sort_order": int(data.get("sort_order", 0) or 0),
    }
    conn = db.get_db()
    with conn:
        db.upsert_menu_item(conn, payload)
    conn.close()
    events.emit("menu.updated", {})
    return jsonify(ok=True, redirect="/admin/menu?notice=saved")


@app.route("/api/admin/menu/<int:item_id>", methods=["POST", "DELETE"])
@require_admin_api
def api_admin_menu_delete(item_id):
    data = request.get_json(force=True, silent=True) or {}
    method = (data.get("_method") or request.form.get("_method") or request.method).upper()
    if method != "DELETE":
        return jsonify(ok=False, message="Method not allowed"), 405
    conn = db.get_db()
    try:
        with conn:
            db.delete_menu_item(conn, item_id)
    except Exception:
        conn.close()
        return jsonify(ok=False, message="無法刪除：此商品已被訂單引用"), 400
    conn.close()
    events.emit("menu.updated", {"deletedId": item_id})
    return jsonify(ok=True, redirect="/admin/menu?notice=deleted")


# ---------------------------------------------------------------------------
# 後台 — 分類管理
# ---------------------------------------------------------------------------

@app.route("/admin/categories", methods=["GET"])
@require_admin
def admin_categories():
    conn = db.get_db()
    categories = db.get_categories(conn)
    edit_id = request.args.get("edit", type=int)
    edit_category = next((c for c in categories if c["id"] == edit_id), None) if edit_id else None
    conn.close()
    return render_template("admin/categories.html",
                           categories=categories, edit_category=edit_category)


@app.route("/api/admin/categories", methods=["POST"])
@require_admin_api
def api_admin_categories_upsert():
    data = request.get_json(force=True, silent=True) or request.form.to_dict()
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify(ok=False, message="分類名稱必填"), 400
    payload = {
        "id": int(data["id"]) if data.get("id") else None,
        "name": name,
        "sort_order": int(data.get("sort_order", 0) or 0),
    }
    conn = db.get_db()
    with conn:
        db.upsert_category(conn, payload)
    conn.close()
    return jsonify(ok=True, redirect="/admin/categories")


@app.route("/api/admin/categories/<int:cat_id>", methods=["POST", "DELETE"])
@require_admin_api
def api_admin_categories_delete(cat_id):
    data = request.get_json(force=True, silent=True) or {}
    method = (data.get("_method") or request.form.get("_method") or request.method).upper()
    if method != "DELETE":
        return jsonify(ok=False, message="Method not allowed"), 405
    conn = db.get_db()
    with conn:
        db.delete_category(conn, cat_id)
    conn.close()
    return jsonify(ok=True, redirect="/admin/categories")


# ---------------------------------------------------------------------------
# 後台 — 桌位管理
# ---------------------------------------------------------------------------

@app.route("/admin/tables", methods=["GET"])
@require_admin
def admin_tables():
    conn = db.get_db()
    tables = db.get_tables(conn)
    edit_id = request.args.get("edit", type=int)
    edit_table = next((t for t in tables if t["id"] == edit_id), None) if edit_id else None
    notice = request.args.get("notice", "")
    conn.close()
    return render_template("admin/tables.html",
                           tables=tables, edit_table=edit_table, notice=notice)


@app.route("/api/admin/tables", methods=["POST"])
@require_admin_api
def api_admin_tables_upsert():
    data = request.get_json(force=True, silent=True) or request.form.to_dict()
    import re
    slug = re.sub(r"[^a-z0-9\-_]", "", re.sub(r"\s+", "-", str(data.get("slug", "")).strip().lower()))
    name = str(data.get("name", "")).strip()
    if not name or not slug:
        return jsonify(ok=False, message="桌號名稱與 slug 都必填"), 400
    payload = {
        "id": int(data["id"]) if data.get("id") else None,
        "name": name,
        "slug": slug,
        # 修正 PostgreSQL 布林值相容性：1/0 轉換為 True/False
        "is_active": data.get("is_active") in ("on", "1", True, 1, "true"),
    }
    conn = db.get_db()
    with conn:
        db.upsert_table(conn, payload)
    conn.close()
    events.emit("tables.updated", {})
    return jsonify(ok=True, redirect="/admin/tables?notice=saved")


@app.route("/api/admin/tables/<int:table_id>", methods=["POST", "DELETE"])
@require_admin_api
def api_admin_tables_delete(table_id):
    data = request.get_json(force=True, silent=True) or {}
    method = (data.get("_method") or request.form.get("_method") or request.method).upper()
    if method != "DELETE":
        return jsonify(ok=False, message="Method not allowed"), 405
    conn = db.get_db()
    try:
        with conn:
            db.delete_table(conn, table_id)
    except Exception:
        conn.close()
        return jsonify(ok=False, message="無法刪除：此桌位已有訂單紀錄"), 400
    conn.close()
    events.emit("tables.updated", {"deletedId": table_id})
    return jsonify(ok=True, redirect="/admin/tables?notice=deleted")


@app.route("/api/admin/tables/<int:table_id>/qr")
@require_admin
def api_admin_table_qr(table_id):
    conn = db.get_db()
    table = db.get_table_by_id(conn, table_id)
    conn.close()
    if not table:
        abort(404)
    #BASE_URL = "https://qr-order-kffxi875t-tomchen-s-projects.vercel.app/"
       
    #target = f"{'BASE_URL'}/t/{table['slug']}"    
    target = f"{public_origin(request)}/t/{table['slug']}"
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(target, image_factory=factory, error_correction=qrcode.constants.ERROR_CORRECT_M)
    buf = io.BytesIO()
    img.save(buf)
    buf.seek(0)
    return Response(buf.read(), mimetype="image/svg+xml")


# ---------------------------------------------------------------------------
# 後台 — 訂單管理
# ---------------------------------------------------------------------------

@app.route("/admin/orders")
@require_admin
def admin_orders():
    conn = db.get_db()
    orders = db.list_orders(conn)
    conn.close()
    return render_template("admin/orders.html", orders=orders)


@app.route("/admin/orders/<int:order_id>")
@require_admin
def admin_order_detail(order_id):
    conn = db.get_db()
    order = db.get_order_by_id(conn, order_id)
    if not order:
        conn.close()
        abort(404)
    items = db.get_order_items(conn, order_id)
    payment = db.get_payment_by_order_id(conn, order_id)
    conn.close()
    return render_template("admin/order_detail.html", order=order, items=items, payment=payment)

@app.route("/api/admin/orders/<int:order_id>/status", methods=["POST"])
@require_admin_api
def api_admin_order_status(order_id):
    data = request.get_json(force=True, silent=True) or request.form.to_dict()
    status = str(data.get("status", "pending"))
    allowed = {"pending", "preparing", "ready", "completed", "cancelled"}
    if status not in allowed:
        return jsonify(ok=False, message="狀態值無效"), 400
    conn = db.get_db()
    order = db.get_order_by_id(conn, order_id)
    if not order:
        conn.close()
        return jsonify(ok=False, message="找不到訂單"), 404
    with conn:
        db.update_order_status(conn, order_id, status)
    conn.close()
    events.emit("order.status.updated", {"orderId": order_id, "status": status})
    return jsonify(ok=True)


@app.route("/api/admin/orders/<int:order_id>", methods=["DELETE"])
@require_admin_api
def api_admin_order_delete(order_id):
    conn = db.get_db()
    order = db.get_order_by_id(conn, order_id)
    if not order:
        conn.close()
        return jsonify(ok=False, message="找不到訂單"), 404
    with conn:
        db.delete_order(conn, order_id)
    conn.close()
    events.emit("order.deleted", {"orderId": order_id})
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# 404 處理
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


# ---------------------------------------------------------------------------
# 啟動
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    from payment_service import get_local_ip
    local_ip = get_local_ip()
    print("=" * 60)
    print("  QR Code Ordering System is running!")
    print(f"  Local PC Access:  http://localhost:{port}")
    print(f"  Mobile Access:    http://{local_ip}:{port}")
    print("  (Make sure your phone is connected to the SAME Wi-Fi network)")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

