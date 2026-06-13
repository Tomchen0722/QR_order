"""
資料庫模組 — SQLite，使用標準庫 sqlite3。
所有 DB 操作集中在此，供 app.py 呼叫。
"""
import sqlite3
import os
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "data.sqlite"


def get_db() -> sqlite3.Connection:
    """取得一個 row_factory=sqlite3.Row 的連線（每次呼叫建立新連線）。"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def init_db():
    conn = get_db()
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS restaurant_tables (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                slug       TEXT    NOT NULL UNIQUE,
                is_active  INTEGER NOT NULL DEFAULT 1,
                created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS menu_categories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS menu_items (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id  INTEGER,
                name         TEXT    NOT NULL,
                description  TEXT    NOT NULL DEFAULT '',
                price        INTEGER NOT NULL,
                image_url    TEXT    NOT NULL DEFAULT '',
                is_available INTEGER NOT NULL DEFAULT 1,
                sort_order   INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES menu_categories(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number       TEXT    NOT NULL DEFAULT '',
                table_id           INTEGER NOT NULL,
                customer_name      TEXT    NOT NULL DEFAULT '',
                note               TEXT    NOT NULL DEFAULT '',
                status             TEXT    NOT NULL DEFAULT 'pending',
                payment_status     TEXT    NOT NULL DEFAULT 'unpaid',
                payment_provider   TEXT    NOT NULL DEFAULT '',
                payment_reference  TEXT    NOT NULL DEFAULT '',
                paid_at            TEXT,
                total              INTEGER NOT NULL DEFAULT 0,
                created_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (table_id) REFERENCES restaurant_tables(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id     INTEGER NOT NULL,
                menu_item_id INTEGER NOT NULL,
                item_name    TEXT    NOT NULL,
                unit_price   INTEGER NOT NULL,
                quantity     INTEGER NOT NULL,
                subtotal     INTEGER NOT NULL,
                FOREIGN KEY (order_id)     REFERENCES orders(id)     ON DELETE CASCADE,
                FOREIGN KEY (menu_item_id) REFERENCES menu_items(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS payments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id     INTEGER NOT NULL UNIQUE,
                provider     TEXT    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'pending',
                amount       INTEGER NOT NULL,
                currency     TEXT    NOT NULL DEFAULT 'TWD',
                reference    TEXT    NOT NULL DEFAULT '',
                checkout_url TEXT    NOT NULL DEFAULT '',
                raw_payload  TEXT    NOT NULL DEFAULT '',
                created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            );
        """)

        # 遷移：補欄位（若舊資料庫缺少）
        _add_column_if_missing(conn, "orders", "order_number", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "orders", "payment_status",    "TEXT NOT NULL DEFAULT 'unpaid'")
        _add_column_if_missing(conn, "orders", "payment_provider",  "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "orders", "payment_reference", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "orders", "paid_at",           "TEXT")

        # 為現有訂單補 order_number（若有缺失）
        _backfill_order_numbers(conn)

        # 確保 order_number 唯一索引存在（必須在 backfill 之後）
        _ensure_unique_index(conn, "orders", "order_number")

        # 種子資料
        _seed(conn)
    conn.close()


def _add_column_if_missing(conn, table, column, definition):
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_unique_index(conn, table, column):
    index_name = f"idx_{table}_{column}"
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
    ).fetchone()
    if not existing:
        conn.execute(f"CREATE UNIQUE INDEX {index_name} ON {table}({column})")


def _normalize_order_number(order_number: str) -> str:
    """將訂單編號統一為 YYYYMMDDXXXX 格式（12碼純數字）。
    若格式不正確，嘗試修正；無法修正則從今天 0001 重新開始。"""
    today = datetime.now().strftime("%Y%m%d")
    if not order_number or not isinstance(order_number, str):
        return f"{today}0001"
    # 移除所有非數字字元（如 dash）
    digits = "".join(c for c in order_number if c.isdigit())
    if len(digits) == 12 and digits[:8] == today:
        return digits
    # 舊格式帶 dash：20260601-0001 → 202606010001
    if len(digits) == 12:
        return digits
    # 長度不對，重新產生
    return f"{today}0001"


def _generate_order_number(conn):
    """產生唯一訂單編號，格式：YYYYMMDDXXXX（XXXX 為 0001 起的流水號）。
    確保回傳值一定是 12 碼純數字。"""
    today = datetime.now().strftime("%Y%m%d")
    row = conn.execute(
        "SELECT order_number FROM orders ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        last = _normalize_order_number(row["order_number"])
        if last[:8] == today and last[8:].isdigit():
            next_seq = int(last[-4:]) + 1
        else:
            next_seq = 1
    else:
        next_seq = 1
    result = f"{today}{next_seq:04d}"
    assert len(result) == 12 and result.isdigit(), f"訂單編號格式錯誤：{result}"
    return result


def _backfill_order_numbers(conn):
    """為現有 order_number 為空的訂單補上編號。"""
    rows = conn.execute("SELECT id FROM orders WHERE order_number='' OR order_number IS NULL").fetchall()
    if not rows:
        return
    for row in rows:
        order_id = row["id"]
        candidate = _generate_order_number(conn)
        conn.execute("UPDATE orders SET order_number=? WHERE id=?", (candidate, order_id))


def _seed(conn):
    if conn.execute("SELECT COUNT(*) FROM restaurant_tables").fetchone()[0] == 0:
        tables = [("A1","a1"),("A2","a2"),("A3","a3"),("B1","b1"),("B2","b2"),("VIP-01","vip-01")]
        conn.executemany("INSERT INTO restaurant_tables (name, slug) VALUES (?,?)", tables)

    if conn.execute("SELECT COUNT(*) FROM menu_categories").fetchone()[0] == 0:
        cats = [("主餐",1),("炸物",2),("飲品",3),("甜點",4)]
        for name, sort in cats:
            conn.execute("INSERT INTO menu_categories (name, sort_order) VALUES (?,?)", (name, sort))

        ids = [r[0] for r in conn.execute("SELECT id FROM menu_categories ORDER BY sort_order")]
        items = [
            (ids[0],"炙燒牛肉丼","香氣十足的炙燒牛肉，搭配溫泉蛋與時蔬。",268,"",1,1),
            (ids[0],"唐揚雞咖哩飯","外酥內嫩的唐揚雞，佐濃郁日式咖哩。",238,"",1,2),
            (ids[0],"松露野菇露野菇燉飯","綿滑米香與松露香氣，素食可食。",248,"",1,3),
            (ids[1],"酥炸脆薯","外皮金黃，適合分享。",88,"",1,1),
            (ids[1],"起司雞塊","起司控必點，趁熱享用口感最好。",118,"",1,2),
            (ids[2],"古早味紅茶","冰涼順口，甜度固定。",45,"",1,1),
            (ids[2],"檸檬氣泡飲","清爽酸甜，適合搭配炸物。",65,"",1,2),
            (ids[2],"拿鐵咖啡","中焙咖啡豆搭配細緻奶泡。",95,"",1,3),
            (ids[3],"焦糖布丁","滑順布丁與焦糖香氣。",58,"",1,1),
            (ids[3],"抹茶巴斯克","濃郁起司與抹茶尾韻。",128,"",1,2),
        ]
        conn.executemany(
            "INSERT INTO menu_items (category_id,name,description,price,image_url,is_available,sort_order) VALUES (?,?,?,?,?,?,?)",
            items
        )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def money(value: int) -> str:
    """格式化為台幣字串，例如 NT$268"""
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        v = 0
    return f"NT${v:,}"


def row_to_dict(row) -> dict:
    return dict(row) if row else None


def rows_to_list(rows) -> list:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 菜單
# ---------------------------------------------------------------------------

def get_all_menu_items(conn):
    return rows_to_list(conn.execute("""
        SELECT mi.*, mc.name AS category_name, mc.sort_order AS category_sort
        FROM menu_items mi
        LEFT JOIN menu_categories mc ON mc.id = mi.category_id
        ORDER BY COALESCE(mc.sort_order,999), COALESCE(mi.sort_order,999), mi.id
    """).fetchall())


def grouped_menu_items(conn) -> list:
    rows = get_all_menu_items(conn)
    groups = []
    seen = {}
    for row in rows:
        key = row["category_id"] or 0
        if key not in seen:
            g = {
                "id": row["category_id"],
                "name": row["category_name"] or "未分類",
                "sort_order": row["category_sort"] if row["category_sort"] is not None else 999,
                "menu_list": []
            }
            seen[key] = g
            groups.append(g)
        seen[key]["menu_list"].append(row)
    return groups


def get_menu_item_by_id(conn, item_id: int):
    return row_to_dict(conn.execute("SELECT * FROM menu_items WHERE id=? LIMIT 1", (item_id,)).fetchone())


def get_categories(conn):
    return rows_to_list(conn.execute("SELECT * FROM menu_categories ORDER BY sort_order, id").fetchall())


def upsert_menu_item(conn, payload: dict) -> int:
    if payload.get("id"):
        conn.execute("""
            UPDATE menu_items
            SET category_id=?, name=?, description=?, price=?, image_url=?, is_available=?, sort_order=?
            WHERE id=?
        """, (
            payload.get("category_id"),
            payload["name"],
            payload.get("description", ""),
            payload["price"],
            payload.get("image_url", ""),
            1 if payload.get("is_available") else 0,
            payload.get("sort_order", 0),
            payload["id"]
        ))
        return payload["id"]
    cur = conn.execute("""
        INSERT INTO menu_items (category_id,name,description,price,image_url,is_available,sort_order)
        VALUES (?,?,?,?,?,?,?)
    """, (
        payload.get("category_id"),
        payload["name"],
        payload.get("description", ""),
        payload["price"],
        payload.get("image_url", ""),
        1 if payload.get("is_available") else 0,
        payload.get("sort_order", 0),
    ))
    return cur.lastrowid


def delete_menu_item(conn, item_id: int):
    conn.execute("DELETE FROM menu_items WHERE id=?", (item_id,))


def upsert_category(conn, payload: dict) -> int:
    if payload.get("id"):
        conn.execute("UPDATE menu_categories SET name=?, sort_order=? WHERE id=?",
                     (payload["name"], payload.get("sort_order", 0), payload["id"]))
        return payload["id"]
    cur = conn.execute("INSERT INTO menu_categories (name, sort_order) VALUES (?,?)",
                       (payload["name"], payload.get("sort_order", 0)))
    return cur.lastrowid


def delete_category(conn, cat_id: int):
    conn.execute("UPDATE menu_items SET category_id=NULL WHERE category_id=?", (cat_id,))
    conn.execute("DELETE FROM menu_categories WHERE id=?", (cat_id,))


# ---------------------------------------------------------------------------
# 桌位
# ---------------------------------------------------------------------------

def get_tables(conn):
    return rows_to_list(conn.execute("SELECT * FROM restaurant_tables ORDER BY id").fetchall())


def get_table_by_slug(conn, slug: str):
    return row_to_dict(conn.execute("SELECT * FROM restaurant_tables WHERE slug=? LIMIT 1", (slug,)).fetchone())


def get_table_by_id(conn, table_id: int):
    return row_to_dict(conn.execute("SELECT * FROM restaurant_tables WHERE id=? LIMIT 1", (table_id,)).fetchone())


def upsert_table(conn, payload: dict) -> int:
    if payload.get("id"):
        conn.execute("UPDATE restaurant_tables SET name=?, slug=?, is_active=? WHERE id=?",
                     (payload["name"], payload["slug"], 1 if payload.get("is_active") else 0, payload["id"]))
        return payload["id"]
    cur = conn.execute("INSERT INTO restaurant_tables (name, slug, is_active) VALUES (?,?,?)",
                       (payload["name"], payload["slug"], 1 if payload.get("is_active") else 0))
    return cur.lastrowid


def delete_table(conn, table_id: int):
    conn.execute("DELETE FROM restaurant_tables WHERE id=?", (table_id,))


# ---------------------------------------------------------------------------
# 訂單
# ---------------------------------------------------------------------------

def list_orders(conn):
    return rows_to_list(conn.execute("""
        SELECT o.*, rt.name AS table_name, rt.slug AS table_slug,
               COUNT(oi.id) AS item_count
        FROM orders o
        JOIN restaurant_tables rt ON rt.id = o.table_id
        LEFT JOIN order_items oi ON oi.order_id = o.id
        GROUP BY o.id
        ORDER BY o.id DESC
    """).fetchall())


def list_kitchen_orders(conn):
    orders = rows_to_list(conn.execute("""
        SELECT o.*, rt.name AS table_name, rt.slug AS table_slug,
               COUNT(oi.id) AS item_count
        FROM orders o
        JOIN restaurant_tables rt ON rt.id = o.table_id
        LEFT JOIN order_items oi ON oi.order_id = o.id
        WHERE o.status IN ('pending','preparing','ready')
        GROUP BY o.id
        ORDER BY CASE o.status WHEN 'pending' THEN 1 WHEN 'preparing' THEN 2 WHEN 'ready' THEN 3 ELSE 4 END, o.id ASC
    """).fetchall())
    for order in orders:
        order["order_items"] = rows_to_list(conn.execute(
            "SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order["id"],)
        ).fetchall())
    return orders


def get_order_by_id(conn, order_id):
    return row_to_dict(conn.execute("""
        SELECT o.*, rt.name AS table_name, rt.slug AS table_slug
        FROM orders o
        JOIN restaurant_tables rt ON rt.id = o.table_id
        WHERE o.id=? LIMIT 1
    """, (order_id,)).fetchone())


def get_order_items(conn, order_id: int):
    return rows_to_list(conn.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order_id,)).fetchall())


def create_order(conn, table_id: int, customer_name: str, note: str, items: list) -> dict:
    """建立訂單，items = [{"menu_item_id": int, "quantity": int}, ...]
    回傳 {"order_id": int, "order_number": str}
    """
    table = get_table_by_id(conn, table_id)
    if not table:
        raise ValueError("找不到桌號")
    if not items:
        raise ValueError("購物車是空的")

    total = 0
    prepared = []
    for item in items:
        mi = get_menu_item_by_id(conn, item["menu_item_id"])
        if not mi or not mi["is_available"]:
            raise ValueError(f"商品無法下單：{item['menu_item_id']}")
        qty = max(1, int(item["quantity"]))
        subtotal = mi["price"] * qty
        total += subtotal
        prepared.append((mi["id"], mi["name"], mi["price"], qty, subtotal))

    order_number = _generate_order_number(conn)
    order_number = _normalize_order_number(order_number)
    if len(order_number) != 12 or not order_number.isdigit():
        raise ValueError(f"訂單編號格式錯誤：{order_number}")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO orders (order_number,table_id,customer_name,note,status,payment_status,total,created_at,updated_at) VALUES (?,?,?,?,'pending','unpaid',?,?,?)",
        (order_number, table_id, customer_name or "", note or "", total, now_str, now_str)
    )
    order_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO order_items (order_id,menu_item_id,item_name,unit_price,quantity,subtotal) VALUES (?,?,?,?,?,?)",
        [(order_id, *p) for p in prepared]
    )
    return {"order_id": order_id, "order_number": order_number}


def update_order_status(conn, order_id: int, status: str):
    conn.execute("UPDATE orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, order_id))


def delete_order(conn, order_id: int):
    conn.execute("DELETE FROM order_items WHERE order_id=?", (order_id,))
    conn.execute("DELETE FROM payments WHERE order_id=?", (order_id,))
    conn.execute("DELETE FROM orders WHERE id=?", (order_id,))


def update_order_payment_status(conn, order_id: int, payment_status: str, provider="", reference="", paid_at=None):
    conn.execute("""
        UPDATE orders
        SET payment_status=?,
            payment_provider=COALESCE(NULLIF(?,''), payment_provider),
            payment_reference=COALESCE(NULLIF(?,''), payment_reference),
            paid_at=COALESCE(?, paid_at),
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    """, (payment_status, provider, reference, paid_at, order_id))


# ---------------------------------------------------------------------------
# 付款
# ---------------------------------------------------------------------------

def get_payment_by_order_id(conn, order_id: int):
    return row_to_dict(conn.execute("SELECT * FROM payments WHERE order_id=? LIMIT 1", (order_id,)).fetchone())


def get_payment_by_reference(conn, reference: str):
    return row_to_dict(conn.execute("SELECT * FROM payments WHERE reference=? LIMIT 1", (reference,)).fetchone())


def upsert_payment(conn, order_id: int, provider: str, status: str = "pending",
                   amount: int = 0, currency: str = "TWD", reference: str = "",
                   checkout_url: str = "", raw_payload: str = "") -> dict:
    existing = get_payment_by_order_id(conn, order_id)
    if existing:
        conn.execute("""
            UPDATE payments
            SET provider=?, status=?, amount=?, currency=?, reference=?, checkout_url=?, raw_payload=?, updated_at=CURRENT_TIMESTAMP
            WHERE order_id=?
        """, (provider, status, amount, currency, reference, checkout_url, raw_payload, order_id))
        return get_payment_by_order_id(conn, order_id)
    cur = conn.execute("""
        INSERT INTO payments (order_id,provider,status,amount,currency,reference,checkout_url,raw_payload)
        VALUES (?,?,?,?,?,?,?,?)
    """, (order_id, provider, status, amount, currency, reference, checkout_url, raw_payload))
    return row_to_dict(conn.execute("SELECT * FROM payments WHERE id=? LIMIT 1", (cur.lastrowid,)).fetchone())


def mark_payment_paid(conn, order_id: int, provider: str = "", reference: str = "",
                      paid_at: str = None, raw_payload: str = None):
    from datetime import datetime, timezone
    payment = get_payment_by_order_id(conn, order_id)
    if not payment:
        raise ValueError("找不到付款紀錄")
    conn.execute("""
        UPDATE payments
        SET status='paid',
            reference=COALESCE(NULLIF(?,''), reference),
            raw_payload=COALESCE(NULLIF(?,''), raw_payload),
            updated_at=CURRENT_TIMESTAMP
        WHERE order_id=?
    """, (reference, raw_payload or "", order_id))
    _paid_at = paid_at or datetime.now(timezone.utc).isoformat()
    update_order_payment_status(conn, order_id, "paid",
                                provider=provider or payment["provider"],
                                reference=reference or payment["reference"],
                                paid_at=_paid_at)


def mark_payment_failed(conn, order_id: int, provider: str = "", reference: str = "", raw_payload: str = None):
    payment = get_payment_by_order_id(conn, order_id)
    if not payment:
        return
    conn.execute("""
        UPDATE payments
        SET status='failed',
            raw_payload=COALESCE(NULLIF(?,''), raw_payload),
            updated_at=CURRENT_TIMESTAMP
        WHERE order_id=?
    """, (raw_payload or "", order_id))
    update_order_payment_status(conn, order_id, "failed",
                                provider=provider or payment["provider"],
                                reference=reference or payment["reference"])


# ---------------------------------------------------------------------------
# 統計
# ---------------------------------------------------------------------------

def get_dashboard_stats(conn) -> dict:
    def scalar(sql):
        return conn.execute(sql).fetchone()[0]
    return {
        "tables":        scalar("SELECT COUNT(*) FROM restaurant_tables"),
        "items":         scalar("SELECT COUNT(*) FROM menu_items"),
        "orders":        scalar("SELECT COUNT(*) FROM orders"),
        "pendingOrders": scalar("SELECT COUNT(*) FROM orders WHERE status IN ('pending','preparing')"),
        "paidOrders":    scalar("SELECT COUNT(*) FROM orders WHERE payment_status='paid'"),
        "revenue":       scalar("SELECT COALESCE(SUM(total),0) FROM orders WHERE payment_status='paid'"),
    }
