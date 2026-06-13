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


def list_kitchen_orders(conn):
    """取得廚房專用訂單"""
    with conn.cursor() as cur:


def money(value):
    try: 
        return f"${int(value):,}"
    except: 
        return f"${value}"
