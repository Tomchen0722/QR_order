"""認證模組 — HMAC cookie 驗證，與原 Node.js 版邏輯相同。"""
import hmac
import hashlib
import os
from functools import wraps
from flask import request, redirect, url_for, make_response


def admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "admin123")


def admin_secret() -> str:
    return os.environ.get("ADMIN_SECRET", "change-this-secret")


def admin_token() -> str:
    return hmac.new(
        admin_secret().encode(),
        admin_password().encode(),
        hashlib.sha256
    ).hexdigest()


def is_admin_request() -> bool:
    cookie = request.cookies.get("admin_auth", "")
    return hmac.compare_digest(cookie, admin_token())


def require_admin(f):
    """頁面用裝飾器，未登入導向 /admin/login。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin_request():
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def require_admin_api(f):
    """API 用裝飾器，未登入回傳 401 JSON。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin_request():
            from flask import jsonify
            return jsonify(ok=False, message="未登入或權限不足"), 401
        return f(*args, **kwargs)
    return decorated
