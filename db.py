"""
資料庫模組 — PostgreSQL (Supabase 版本)，使用庫 psycopg2。
所有 DB 操作集中在此，供 app.py 呼叫。
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime

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


def init_db():
    """初始化資料表"""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS restaurant_tables (
                id         SERIAL PRIMARY KEY,
                name       TEXT    NOT NULL,
                slug       TEXT    NOT NULL UNIQUE,
                is_active  INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS menu_categories (
                id         SERIAL PRIMARY KEY,
                name       TEXT    NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS menu_items (
                id           SERIAL PRIMARY KEY,
                category_id  INTEGER,
                name         TEXT    NOT NULL,
                description  TEXT    NOT NULL DEFAULT '',
                price        INTEGER NOT NULL,
                image_url    TEXT    NOT NULL DEFAULT '',
                is_available INTEGER NOT NULL DEFAULT 1,
                sort_order   INTEGER NOT NULL DEFAULT 0,
                created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES menu_categories(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                id                 SERIAL PRIMARY KEY,
                order_number       TEXT    NOT NULL DEFAULT '',
                table_id           INTEGER NOT NULL,
                customer_name      TEXT    NOT NULL DEFAULT '',
                note               TEXT    NOT NULL DEFAULT '',
                status             TEXT    NOT NULL DEFAULT 'pending',
                payment_status     TEXT    NOT NULL DEFAULT 'unpaid',
                payment_provider   TEXT    NOT NULL DEFAULT '',
                payment_reference  TEXT    NOT NULL DEFAULT '',
                paid_at            TIMESTAMP,
                total              INTEGER NOT NULL DEFAULT 0,
                created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (table_id) REFERENCES restaurant_tables(id) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id           SERIAL PRIMARY KEY,
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
                id           SERIAL PRIMARY KEY,
                order_id     INTEGER NOT NULL UNIQUE,
                provider     TEXT    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'pending',
                amount       INTEGER NOT NULL,
                currency     TEXT    NOT NULL DEFAULT 'TWD',
                reference    TEXT    NOT NULL DEFAULT '',
                checkout_url TEXT    NOT NULL DEFAULT '',
                raw_payload  TEXT    NOT NULL DEFAULT '',
                created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            );
        """)
        _add_column_if_missing(cur, "orders", "order_number", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(cur, "orders", "payment_status", "TEXT NOT NULL DEFAULT 'unpaid'")
        _add_column_if_missing(cur, "orders", "payment_provider", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(cur, "orders", "payment_reference", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(cur, "orders", "paid_at", "TIMESTAMP")
        _backfill_order_numbers(cur)
        _ensure_unique_index(cur, "orders", "order_number")
        _seed(cur)
    conn.commit()
    conn.close()


def _add_column_if_missing(cur, table, column, definition):
    cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}' AND column_name='{column}';")
    if not cur.fetchone():
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_unique_index(cur, table, column):
    index_name = f"idx_{table}_{column}"
    cur.execute(f"SELECT indexname FROM pg_indexes WHERE indexname = '{index_name}';")
    if not cur.fetchone():
        cur.execute(f"CREATE UNIQUE INDEX {index_name} ON {table}({column})")


def _normalize_order_number(order_number: str) -> str:
    today = datetime.now().strftime("%Y%m%d")
    if not order_number or not isinstance(order_number, str):
        return f"{today}0001"
    digits = "".join(c for c in order_number if c.isdigit())
    if len(digits) == 12:
        return digits
    return f"{today}0001"


def _generate_order_number(cur):
    today = datetime.now().strftime("%Y%m%d")
    cur.execute("SELECT order_number FROM orders ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if row:
        last = _normalize_order_number(row["order_number"])
        if last[:8] == today and last[8:].isdigit():
            next_seq = int(last[-4:]) + 1
        else:
            next_seq = 1
    else:
        next_seq = 1
    return f"{today}{next_seq:04d}"


def _backfill_order_numbers(cur):
    cur.execute("SELECT id FROM orders WHERE order_number='' OR order_number IS NULL")
    rows = cur.fetchall()
    if not rows:
        return
    for row in rows:
        cur.execute("UPDATE orders SET order_number=%s WHERE id=%s", (_generate_order_number(cur), row["id"]))


def _seed(cur):
    cur.execute("SELECT COUNT(*) FROM restaurant_tables")
    if cur.fetchone()['count'] == 0:
        tables = [("A1","a1"),("A2","a2"),("A3","a3"),("B1","b1"),("B2","b2"),("VIP-01","vip-01")]
        cur.executemany("INSERT INTO restaurant_tables (name, slug) VALUES (%s, %s)", tables)

    cur.execute("SELECT COUNT(*) FROM menu_categories")
    if cur.fetchone()['count'] == 0:
        cats = [("主餐",1),("炸物",2),("飲品",3),("甜點",4)]
        for name, sort in cats:
            cur.execute("INSERT INTO menu_categories (name, sort_order) VALUES (%s, %s)", (name, sort))
        cur.execute("SELECT id FROM menu_categories ORDER BY sort_order")
        ids = [r['id'] for r in cur.fetchall()]
        items = [
            (ids[0],"炙燒牛肉丼","香氣十足的炙燒牛肉，搭配溫泉蛋與時蔬。",268,"",1,1),
            (ids[0],"唐揚雞咖哩飯","外酥內嫩的唐揚雞，佐濃郁日式咖哩。",238,"",1,2),
            (ids[0],"松露野菇燉飯","綿滑米香與松露香氣，素食可食。",248,"",1,3),
            (ids[1],"酥炸脆薯","外皮金黃，適合分享。",88,"",1,1),
        ]
        cur.executemany("INSERT INTO menu_items (category_id, name, description, price, image_url, is_available, sort_order) VALUES (%s, %s, %s, %s, %s, %s, %s)", items)


# ---------------------------------------------------------------------------
# 各頁面核心查詢與操作函式
# ---------------------------------------------------------------------------

def get_tables(conn):
    """取得所有桌位列表"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM restaurant_tables ORDER BY id ASC;")
        return cur.fetchall()


def get_dashboard_stats(conn):
    """取得儀表板統計數據（今日訂單數、今日營業額、待製作訂單）"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    stats = {"today_orders": 0, "revenue": 0, "pending_orders": 0}
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM orders WHERE created_at::text LIKE %s;", (f"{today_str}%",))
        row = cur.fetchone()
        if row: stats["today_orders"] = int(row.get("count") or 0)
        cur.execute("SELECT SUM(total) FROM orders WHERE payment_status = 'paid' AND created_at::text LIKE %s;", (f"{today_str}%",))
        row = cur.fetchone()
        stats["revenue"] = int(row.get("sum") or 0) if row and row.get("sum") else 0
        cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending';")
        row = cur.fetchone()
        if row: stats["pending_orders"] = int(row.get("count") or 0)
    return stats


def get_categories(conn):
    """取得所有分類"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM menu_categories ORDER BY sort_order ASC, id ASC;")
        return cur.fetchall()


def grouped_menu_items(conn):
    """取得分類分組選單"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM menu_categories ORDER BY sort_order ASC, id ASC;")
        categories = cur.fetchall()
        cur.execute("SELECT * FROM menu_items ORDER BY sort_order ASC, id ASC;")
        all_items = cur.fetchall()
        result = []
        for cat in categories:
            result.append({
                "id": cat["id"], "name": cat["name"], "sort_order": cat["sort_order"],
                "menu_list": [dict(item) for item in all_items if item["category_id"] == cat["id"]]
            })
        return result


def list_orders(conn):
    """取得所有訂單"""
    with conn.cursor() as cur:
        cur.execute("SELECT o.*, t.name as table_name FROM orders o JOIN restaurant_tables t ON o.table_id = t.id ORDER BY o.id DESC;")
        return cur.fetchall()

def money(value):
    try:
        return f"${float(value):,.0f}"
    except:
        return "$0"

def list_kitchen_orders(conn):
    
    with conn.cursor() as cur:

        cur.execute("""
            SELECT
                o.*,
                t.name AS table_name
            FROM orders o
            JOIN restaurant_tables t
                ON o.table_id = t.id
            WHERE o.status IN ('pending','preparing','ready')
            ORDER BY o.created_at DESC
        """)

        orders = cur.fetchall()

        result = []
        for order in orders:
            cur.execute("""
                SELECT
                    item_name,
                    quantity
                FROM order_items
                WHERE order_id = %s
                ORDER BY id
            """, (order["id"],))
            serialized = serialize_dict(order)
            serialized["order_items"] = rows_to_list(cur.fetchall())
            result.append(serialized)

        return result

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

def delete_category(conn, cat_id):
    
    with conn.cursor() as cur:

        cur.execute("""
            DELETE FROM menu_categories
            WHERE id=%s
        """, (cat_id,))

    conn.commit()

def upsert_menu_item(conn, payload):
    is_available = 1 if payload.get("is_available") else 0

    with conn.cursor() as cur:

        if payload.get("id"):

            cur.execute("""
                UPDATE menu_items
                SET
                    category_id=%s,
                    name=%s,
                    description=%s,
                    price=%s,
                    image_url=%s,
                    is_available=%s,
                    sort_order=%s
                WHERE id=%s
            """, (
                payload["category_id"],
                payload["name"],
                payload["description"],
                payload["price"],
                payload["image_url"],
                is_available,
                payload["sort_order"],
                payload["id"]
            ))

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

def delete_menu_item(conn, item_id):
    
    with conn.cursor() as cur:

        cur.execute("""
            DELETE FROM menu_items
            WHERE id=%s
        """, (item_id,))

    conn.commit()

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
                is_active,
                payload["id"]
            ))

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
                    %s,%s,%s
                )
            """, (
                payload["name"],
                payload["slug"],
                is_active
            ))

    conn.commit()

def delete_table(conn, table_id):
    
    with conn.cursor() as cur:

        cur.execute("""
            DELETE FROM restaurant_tables
            WHERE id=%s
        """, (table_id,))

    conn.commit()

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

def get_payment_by_order_id(conn, order_id):
    
    with conn.cursor() as cur:

        cur.execute("""
            SELECT *
            FROM payments
            WHERE order_id=%s
        """, (order_id,))

        return cur.fetchone()

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

        order_number = _generate_order_number(cur)

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
# ---------------------------------------------------------------------------

def upsert_payment(conn, order_id, provider, status, amount, currency, reference, checkout_url, raw_payload):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO payments (
                order_id, provider, status, amount, currency,
                reference, checkout_url, raw_payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (order_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                status = EXCLUDED.status,
                amount = EXCLUDED.amount,
                currency = EXCLUDED.currency,
                reference = EXCLUDED.reference,
                checkout_url = EXCLUDED.checkout_url,
                raw_payload = EXCLUDED.raw_payload,
                updated_at = CURRENT_TIMESTAMP
            RETURNING *
        """, (order_id, provider, status, amount, currency, reference, checkout_url, raw_payload))
        row = cur.fetchone()
    conn.commit()
    return row


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


def mark_payment_paid(conn, order_id, provider, reference, paid_at, raw_payload):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE payments
            SET status = 'paid',
                provider = %s,
                reference = %s,
                raw_payload = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE order_id = %s
        """, (provider, reference, raw_payload, order_id))
        cur.execute("""
            UPDATE orders
            SET payment_status = 'paid',
                payment_provider = %s,
                payment_reference = %s,
                paid_at = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (provider, reference, paid_at, order_id))
    conn.commit()


def mark_payment_failed(conn, order_id, provider, reference, raw_payload):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE payments
            SET status = 'failed',
                provider = %s,
                reference = %s,
                raw_payload = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE order_id = %s
        """, (provider, reference, raw_payload, order_id))
        cur.execute("""
            UPDATE orders
            SET payment_status = 'failed',
                payment_provider = %s,
                payment_reference = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (provider, reference, order_id))
    conn.commit()




def delete_order(conn, order_id):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM orders WHERE id = %s", (order_id,))
    conn.commit()

