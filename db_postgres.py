"""
資料庫模組 — PostgreSQL (Supabase 版本)，使用庫 psycopg2。
所有 DB 操作集中在此，供 app.py 呼叫。
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
#from datetime import datetime
from datetime import datetime, timezone


# 讀取 Vercel 設定的 Supabase 連線字串
DATABASE_URL = os.environ.get("DATABASE_URL")

def serialize_dict(row):
    d = dict(row)

    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()

    return d

def rows_to_list(rows):
    return [serialize_dict(r) for r in rows]

def row_to_dict(row):
    if not row:
        return None
    return serialize_dict(row)



def get_db():
    """取得一個支援欄位名稱存取的 PostgreSQL 連線（每次呼叫建立新連線）。"""
    if not DATABASE_URL:
        raise ValueError("環境變數 DATABASE_URL 未設定，請先在 Vercel 後台設定。")
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor_factory = RealDictCursor
    return conn

def fetchall(conn, sql, params=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()

def fetchone(conn, sql, params=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()

def execute(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())

# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------
def init_db():
    conn = get_db()

    try:
        with conn:
            with conn.cursor() as cur:

                # -------------------------
                # 桌位
                # -------------------------
                cur.execute("""
                CREATE TABLE IF NOT EXISTS restaurant_tables (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL UNIQUE,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                """)

                # -------------------------
                # 菜單分類
                # -------------------------
                cur.execute("""
                CREATE TABLE IF NOT EXISTS menu_categories (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0
                );
                """)

                # -------------------------
                # 菜單品項
                # -------------------------
                cur.execute("""
                CREATE TABLE IF NOT EXISTS menu_items (
                    id SERIAL PRIMARY KEY,
                    category_id INTEGER REFERENCES menu_categories(id) ON DELETE SET NULL,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    price INTEGER NOT NULL,
                    image_url TEXT DEFAULT '',
                    is_available INTEGER DEFAULT 1,
                    sort_order INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                """)

                # -------------------------
                # 訂單
                # -------------------------
                cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    order_number TEXT NOT NULL DEFAULT '',
                    table_id INTEGER REFERENCES restaurant_tables(id),
                    customer_name TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    payment_status TEXT DEFAULT 'unpaid',
                    payment_provider TEXT DEFAULT '',
                    payment_reference TEXT DEFAULT '',
                    paid_at TIMESTAMP,
                    total INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """)

                # -------------------------
                # 訂單明細
                # -------------------------
                cur.execute("""
                CREATE TABLE IF NOT EXISTS order_items (
                    id SERIAL PRIMARY KEY,
                    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
                    menu_item_id INTEGER,
                    item_name TEXT NOT NULL,
                    unit_price INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    subtotal INTEGER NOT NULL
                );
                """)

                # -------------------------
                # 付款
                # -------------------------
                cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    order_id INTEGER UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    amount INTEGER NOT NULL,
                    currency TEXT DEFAULT 'TWD',
                    reference TEXT DEFAULT '',
                    checkout_url TEXT DEFAULT '',
                    raw_payload TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """)

        # -------------------------
        # seed + migration（PostgreSQL 版本）
        # -------------------------
        _seed(conn)
        _backfill_order_numbers(conn)
        _ensure_unique_index_pg(conn)

    finally:
        conn.close()
#----------------------

def _add_column_if_missing(conn, table, column, definition):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
        """, (table, column))

        if not cur.fetchone():
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_unique_index(conn, table, column):
    index_name = f"idx_{table}_{column}"

    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1
            FROM pg_indexes
            WHERE tablename = %s AND indexname = %s
        """, (table, index_name))

        if not cur.fetchone():
            cur.execute(f"""
                CREATE UNIQUE INDEX {index_name}
                ON {table} ({column})
            """)


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
    today = datetime.now().strftime("%Y%m%d")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT order_number
            FROM orders
            ORDER BY id DESC
            LIMIT 1
        """)
        row = cur.fetchone()

    if row:
        last = _normalize_order_number(row["order_number"])
        if last[:8] == today and last[8:].isdigit():
            next_seq = int(last[-4:]) + 1
        else:
            next_seq = 1
    else:
        next_seq = 1

    result = f"{today}{next_seq:04d}"

    assert len(result) == 12 and result.isdigit(), f"訂單編號錯誤：{result}"

    return result

def _backfill_order_numbers(conn):
    with conn.cursor() as cur:

        cur.execute("""
            SELECT id
            FROM orders
            WHERE order_number = '' OR order_number IS NULL
        """)

        rows = cur.fetchall()

        for row in rows:
            order_id = row["id"]
            candidate = _generate_order_number(conn)

            cur.execute("""
                UPDATE orders
                SET order_number = %s
                WHERE id = %s
            """, (candidate, order_id))
#-------------------------------------

def _seed(conn):
    with conn.cursor() as cur:

        # -----------------------
        # restaurant_tables
        # -----------------------
        cur.execute("SELECT COUNT(*) FROM restaurant_tables")
        if cur.fetchone()[0] == 0:
            tables = [
                ("A1","a1"),("A2","a2"),("A3","a3"),
                ("B1","b1"),("B2","b2"),("VIP-01","vip-01")
            ]

            cur.executemany("""
                INSERT INTO restaurant_tables (name, slug)
                VALUES (%s, %s)
            """, tables)

        # -----------------------
        # categories
        # -----------------------
        cur.execute("SELECT COUNT(*) FROM menu_categories")
        if cur.fetchone()[0] == 0:

            cats = [("主餐",1),("炸物",2),("飲品",3),("甜點",4)]

            cur.executemany("""
                INSERT INTO menu_categories (name, sort_order)
                VALUES (%s, %s)
            """, cats)

            # 取分類 id
            cur.execute("""
                SELECT id FROM menu_categories ORDER BY sort_order
            """)
            ids = [r["id"] for r in cur.fetchall()]

            # -----------------------
            # menu_items
            # -----------------------
            items = [
                (ids[0],"炙燒牛肉丼","香氣十足的炙燒牛肉，搭配溫泉蛋與時蔬。",268,"",1,1),
                (ids[0],"唐揚雞咖哩飯","外酥內嫩的唐揚雞，佐濃郁日式咖哩。",238,"",1,2),
                (ids[0],"松露野菇燉飯","綿滑米香與松露香氣，素食可食。",248,"",1,3),

                (ids[1],"酥炸脆薯","外皮金黃，適合分享。",88,"",1,1),
                (ids[1],"起司雞塊","起司控必點，趁熱享用口感最好。",118,"",1,2),

                (ids[2],"古早味紅茶","冰涼順口，甜度固定。",45,"",1,1),
                (ids[2],"檸檬氣泡飲","清爽酸甜，適合搭配炸物。",65,"",1,2),
                (ids[2],"拿鐵咖啡","中焙咖啡豆搭配細緻奶泡。",95,"",1,3),

                (ids[3],"焦糖布丁","滑順布丁與焦糖香氣。",58,"",1,1),
                (ids[3],"抹茶巴斯克","濃郁起司與抹茶尾韻。",128,"",1,2),
            ]

            cur.executemany("""
                INSERT INTO menu_items
                (category_id, name, description, price, image_url, is_available, sort_order)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, items)

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def money(value: int) -> str:
    """格式化為台幣，例如 NT$268"""
    try:
        v = int(value or 0)
    except (TypeError, ValueError):
        v = 0
    return f"NT${v:,}"


def row_to_dict(row):
    """psycopg2 RealDictCursor 安全轉換"""
    if row is None:
        return None

    try:
        return dict(row)
    except Exception:
        return row


def rows_to_list(rows):
    """安全轉 list of dict"""
    if not rows:
        return []

    result = []
    for r in rows:
        try:
            result.append(dict(r))
        except Exception:
            result.append(r)

    return result


# ---------------------------------------------------------------------------
# 菜單
# ---------------------------------------------------------------------------

def get_all_menu_items(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT mi.*, mc.name AS category_name, mc.sort_order AS category_sort
            FROM menu_items mi
            LEFT JOIN menu_categories mc ON mc.id = mi.category_id
            ORDER BY COALESCE(mc.sort_order,999),
                     COALESCE(mi.sort_order,999),
                     mi.id
        """)
        return rows_to_list(cur.fetchall())


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
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM menu_items
            WHERE id = %s
            LIMIT 1
        """, (item_id,))

        return row_to_dict(cur.fetchone())


def get_categories(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM menu_categories
            ORDER BY sort_order, id
        """)
        return rows_to_list(cur.fetchall())
#----------------------------------------------------------------------

def upsert_menu_item(conn, payload: dict) -> int:
    with conn.cursor() as cur:

        is_available = bool(payload.get("is_available"))

        # ------------------------
        # UPDATE
        # ------------------------
        if payload.get("id"):
            cur.execute("""
                UPDATE menu_items
                SET category_id=%s,
                    name=%s,
                    description=%s,
                    price=%s,
                    image_url=%s,
                    is_available=%s,
                    sort_order=%s
                WHERE id=%s
            """, (
                payload.get("category_id"),
                payload["name"],
                payload.get("description", ""),
                payload["price"],
                payload.get("image_url", ""),
                is_available,
                payload.get("sort_order", 0),
                payload["id"]
            ))
            return payload["id"]

        else:

            cur.execute("""
                INSERT INTO menu_items
                (
                    category_id,
                    name,
                    description,
                    price,
                    image_url,
                    is_available,
                    sort_order
                )
                VALUES
                (
                    %s,%s,%s,%s,%s,%s,%s
                )
            """, (
                payload["category_id"],
                payload["name"],
                payload["description"],
                payload["price"],
                payload["image_url"],
                is_available,
                payload["sort_order"]
            ))

    conn.commit()

#------------------------------------------------------------
def delete_menu_item(conn, item_id: int):
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM menu_items
            WHERE id = %s
        """, (item_id,))


def upsert_category(conn, payload):
    with conn.cursor() as cur:

        if payload.get("id"):

            cur.execute("""
                UPDATE menu_categories
                SET
                    name=%s,
                    sort_order=%s
                WHERE id=%s
            """, (
                payload["name"],
                payload["sort_order"],
                payload["id"]
            ))

        else:

            cur.execute("""
                INSERT INTO menu_categories
                (
                    name,
                    sort_order
                )
                VALUES
                (
                    %s,
                    %s
                )
            """, (
                payload["name"],
                payload["sort_order"]
            ))

    conn.commit()

#-----------------------------------------------------------------------------------

def delete_category(conn, cat_id: int):
    with conn:
        with conn.cursor() as cur:

            cur.execute("""
                UPDATE menu_items
                SET category_id = NULL
                WHERE category_id = %s
            """, (cat_id,))

            cur.execute("""
                DELETE FROM menu_categories
                WHERE id = %s
            """, (cat_id,))

# ---------------------------------------------------------------------------
# 桌位
# ---------------------------------------------------------------------------

def get_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM restaurant_tables
            ORDER BY id
        """)
        return rows_to_list(cur.fetchall())


def get_table_by_slug(conn, slug):
    
    with conn.cursor() as cur:

        cur.execute("""
            SELECT *
            FROM restaurant_tables
            WHERE slug=%s
        """, (slug,))

        return cur.fetchone()


def get_table_by_id(conn, table_id):
    
    with conn.cursor() as cur:

        cur.execute("""
            SELECT *
            FROM restaurant_tables
            WHERE id=%s
        """, (table_id,))

        return cur.fetchone()


def upsert_table(conn, payload):
    is_active = 1 if payload.get("is_active") else 0

    with conn.cursor() as cur:

        if payload.get("id"):

            cur.execute("""
                UPDATE restaurant_tables
                SET
                    name=%s,
                    slug=%s,
                    is_active=%s
                WHERE id=%s
            """, (
                payload["name"],
                payload["slug"],
                payload.get("is_active", True),
                payload["id"]
            ))

            return payload["id"]
        else:

            cur.execute("""
                INSERT INTO restaurant_tables
                (
                    name,
                    slug,
                    is_active
                )
                VALUES
                (
                    %s,%s,%s::boolean
                )
            """, (
                payload["name"],
                payload["slug"],
                is_active
            ))

    conn.commit()



def delete_table(conn, table_id: int):
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM restaurant_tables
            WHERE id = %s
        """, (table_id,))


# ---------------------------------------------------------------------------
# 訂單
# ---------------------------------------------------------------------------

def list_orders(conn):
    with conn.cursor() as cur:

        cur.execute("""
            SELECT
                o.*,
                rt.name AS table_name,
                rt.slug AS table_slug,
                COUNT(oi.id) AS item_count
            FROM orders o
            JOIN restaurant_tables rt ON rt.id = o.table_id
            LEFT JOIN order_items oi ON oi.order_id = o.id
            GROUP BY o.id, rt.name, rt.slug
            ORDER BY o.id DESC
        """)

        return rows_to_list(cur.fetchall())


def list_kitchen_orders(conn):
    with conn.cursor() as cur:

        cur.execute("""
            SELECT
                o.*,
                rt.name AS table_name,
                rt.slug AS table_slug,
                COUNT(oi.id) AS item_count
            FROM orders o
            JOIN restaurant_tables rt ON rt.id = o.table_id
            LEFT JOIN order_items oi ON oi.order_id = o.id
            WHERE o.status IN ('pending','preparing','ready')
            GROUP BY o.id, rt.name, rt.slug
            ORDER BY
                CASE o.status
                    WHEN 'pending' THEN 1
                    WHEN 'preparing' THEN 2
                    WHEN 'ready' THEN 3
                    ELSE 4
                END,
                o.id ASC
        """)

        orders = rows_to_list(cur.fetchall())

    # 再抓 items（這段 OK，但要 cursor）
    for order in orders:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM order_items
                WHERE order_id = %s
                ORDER BY id
            """, (order["id"],))

            order["order_items"] = rows_to_list(cur.fetchall())

    return orders


def get_order_by_id(conn, order_id):
    
    with conn.cursor() as cur:

        cur.execute("""
            SELECT
                o.*,
                t.name as table_name,
                t.slug as table_slug
            FROM orders o
            JOIN restaurant_tables t
                ON o.table_id=t.id
            WHERE o.id=%s
        """, (order_id,))

        return cur.fetchone()


def get_order_items(conn, order_id):
    
    with conn.cursor() as cur:

        cur.execute("""
            SELECT *
            FROM order_items
            WHERE order_id=%s
        """, (order_id,))

        return cur.fetchall()

#------------------------------------------------


def create_order(conn, table_id, customer_name="", note="", items=None):
    if not items:
        raise ValueError("購物車不可為空")

    with conn.cursor() as cur:

        total = 0
        order_items = []

        for item in items:

            cur.execute("""
                SELECT id,name,price,is_available
                FROM menu_items
                WHERE id=%s
            """, (item["menu_item_id"],))

            menu = cur.fetchone()

            if not menu:
                raise ValueError("商品不存在")

            if not menu["is_available"]:
                raise ValueError(f"{menu['name']} 已停售")

            qty = int(item["quantity"])
            subtotal = menu["price"] * qty

            total += subtotal

            order_items.append({
                "menu_item_id": menu["id"],
                "item_name": menu["name"],
                "unit_price": menu["price"],
                "quantity": qty,
                "subtotal": subtotal
            })

        order_number = _generate_order_number(conn)

        cur.execute("""
            INSERT INTO orders (
                order_number,
                table_id,
                customer_name,
                note,
                total
            )
            VALUES (%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            order_number,
            table_id,
            customer_name,
            note,
            total
        ))

        order_id = cur.fetchone()["id"]

        for item in order_items:

            cur.execute("""
                INSERT INTO order_items (
                    order_id,
                    menu_item_id,
                    item_name,
                    unit_price,
                    quantity,
                    subtotal
                )
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (
                order_id,
                item["menu_item_id"],
                item["item_name"],
                item["unit_price"],
                item["quantity"],
                item["subtotal"]
            ))

    return {
        "order_id": order_id,
        "order_number": order_number
    }



def update_order_status(conn, order_id, status):
    
    with conn.cursor() as cur:

        cur.execute("""
            UPDATE orders
            SET
                status = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (
            status,
            order_id
        ))

    conn.commit()


#----------------------------------------------------------
def delete_order(conn, order_id: int):
    with conn.cursor() as cur:

        # 1. 刪 order items
        cur.execute("""
            DELETE FROM order_items
            WHERE order_id = %s
        """, (order_id,))

        # 2. 刪 payments
        cur.execute("""
            DELETE FROM payments
            WHERE order_id = %s
        """, (order_id,))

        # 3. 刪 orders
        cur.execute("""
            DELETE FROM orders
            WHERE id = %s
        """, (order_id,))

    conn.commit()


def update_order_payment_status(conn, order_id, status, provider="", reference=""):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE orders
            SET payment_status = %s,
                payment_provider = %s,
                payment_reference = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (status, provider, reference, order_id))
    conn.commit()


# ---------------------------------------------------------------------------
# 付款
# ---------------------------------------------------------------------------

def get_payment_by_order_id(conn, order_id: int):
    
    with conn.cursor() as cur:

        cur.execute("""
            SELECT *
            FROM payments
            WHERE order_id=%s
            LIMIT 1
        """, (order_id,))

        row = cur.fetchone()

    return row_to_dict(row) if row else None

def get_payment_by_reference(conn, reference: str):
    
    with conn.cursor() as cur:

        cur.execute("""
            SELECT *
            FROM payments
            WHERE reference=%s
            LIMIT 1
        """, (reference,))

        row = cur.fetchone()

    return row_to_dict(row) if row else None


def upsert_payment(
        conn,
        order_id: int,
        provider: str,
        status: str = "pending",
        amount: int = 0,
        currency: str = "TWD",
        reference: str = "",
        checkout_url: str = "",
        raw_payload: str = ""
):

    existing = get_payment_by_order_id(conn, order_id)

    if existing:

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE payments
                SET provider=%s,
                    status=%s,
                    amount=%s,
                    currency=%s,
                    reference=%s,
                    checkout_url=%s,
                    raw_payload=%s,
                    updated_at=CURRENT_TIMESTAMP
                WHERE order_id=%s
            """, (
                provider,
                status,
                amount,
                currency,
                reference,
                checkout_url,
                raw_payload,
                order_id
            ))

        conn.commit()

        return get_payment_by_order_id(conn, order_id)

    with conn.cursor() as cur:

        cur.execute("""
            INSERT INTO payments
            (
                order_id,
                provider,
                status,
                amount,
                currency,
                reference,
                checkout_url,
                raw_payload
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            order_id,
            provider,
            status,
            amount,
            currency,
            reference,
            checkout_url,
            raw_payload
        ))

        payment_id = cur.fetchone()[0]

    conn.commit()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT *
            FROM payments
            WHERE id=%s
        """, (payment_id,))

        row = cur.fetchone()

    return row_to_dict(row)


def mark_payment_paid(
        conn,
        order_id: int,
        provider: str = "",
        reference: str = "",
        paid_at: str = None,
        raw_payload: str = None
):

    from datetime import datetime, timezone

    payment = get_payment_by_order_id(conn, order_id)

    if not payment:
        raise ValueError("找不到付款紀錄")

    with conn.cursor() as cur:

        cur.execute("""
            UPDATE payments
            SET status='paid',
                reference=COALESCE(NULLIF(%s,''), reference),
                raw_payload=COALESCE(NULLIF(%s,''), raw_payload),
                updated_at=CURRENT_TIMESTAMP
            WHERE order_id=%s
        """, (
            reference,
            raw_payload or "",
            order_id
        ))

    conn.commit()

    _paid_at = paid_at or datetime.now(
        timezone.utc
    ).isoformat()

    update_order_payment_status(
        conn,
        order_id,
        "paid",
        provider=provider or payment["provider"],
        reference=reference or payment["reference"],
        paid_at=_paid_at
    )


def mark_payment_failed(
        conn,
        order_id: int,
        provider: str = "",
        reference: str = "",
        raw_payload: str = None
):

    payment = get_payment_by_order_id(conn, order_id)

    if not payment:
        return

    with conn.cursor() as cur:

        cur.execute("""
            UPDATE payments
            SET status='failed',
                raw_payload=COALESCE(NULLIF(%s,''), raw_payload),
                updated_at=CURRENT_TIMESTAMP
            WHERE order_id=%s
        """, (
            raw_payload or "",
            order_id
        ))

    conn.commit()

    update_order_payment_status(
        conn,
        order_id,
        "failed",
        provider=provider or payment["provider"],
        reference=reference or payment["reference"]
    )
#--------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 統計
# ---------------------------------------------------------------------------

def get_dashboard_stats(conn):
    def scalar(sql):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return row["count"] if row else 0

    return {
        "tables": scalar("SELECT COUNT(*) AS count FROM restaurant_tables"),
        "items": scalar("SELECT COUNT(*) AS count FROM menu_items"),
        "orders": scalar("SELECT COUNT(*) AS count FROM orders"),
        "pendingOrders": scalar("""
            SELECT COUNT(*) AS count
            FROM orders
            WHERE status IN ('pending','preparing')
        """),
        "paidOrders": scalar("""
            SELECT COUNT(*) AS count
            FROM orders
            WHERE payment_status='paid'
        """),
        "revenue": scalar("""
            SELECT COALESCE(SUM(total),0) AS count
            FROM orders
            WHERE payment_status='paid'
        """),
    }
#-----------------------------------------------------------------------------

