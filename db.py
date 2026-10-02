"""Слой работы с Neon (PostgreSQL). Заменяет products/orders/questions/promos .json.

Все функции возвращают словари той же формы, что раньше лежали в JSON-файлах,
поэтому index.html и admin.html менять не нужно.
"""
import os
import datetime
import contextlib

import psycopg2
import psycopg2.extras
from psycopg2.extras import Json, RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Переменная окружения DATABASE_URL (строка подключения Neon) не задана")

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    price       INTEGER NOT NULL DEFAULT 0,
    old_price   INTEGER,
    badge       TEXT,
    sizes       JSONB NOT NULL DEFAULT '[]'::jsonb,
    swatch      INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    images      JSONB NOT NULL DEFAULT '[]'::jsonb,
    image       TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id                SERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    name              TEXT NOT NULL,
    phone             TEXT NOT NULL,
    comment           TEXT NOT NULL DEFAULT '',
    items             JSONB NOT NULL DEFAULT '[]'::jsonb,
    subtotal          INTEGER NOT NULL DEFAULT 0,
    discount          INTEGER NOT NULL DEFAULT 0,
    promo_code        TEXT,
    total             INTEGER NOT NULL DEFAULT 0,
    customer_chat_id  BIGINT,
    customer_username TEXT,
    status            TEXT NOT NULL DEFAULT 'new',
    messages          JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders (customer_chat_id);

CREATE TABLE IF NOT EXISTS questions (
    id                SERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    product_id        INTEGER,
    product_name      TEXT NOT NULL DEFAULT '',
    question          TEXT NOT NULL,
    customer_chat_id  BIGINT,
    customer_username TEXT,
    status            TEXT NOT NULL DEFAULT 'new',
    messages          JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS promos (
    id         SERIAL PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    type       TEXT NOT NULL,
    value      DOUBLE PRECISION NOT NULL,
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    max_uses   INTEGER,
    used_count INTEGER NOT NULL DEFAULT 0,
    min_total  INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# имя поля в API -> имя колонки в таблице (там, где они различаются)
COLUMN_MAP = {"products": {"desc": "description"}}


@contextlib.contextmanager
def conn():
    # Neon усыпляет неактивные соединения, поэтому открываем соединение на запрос.
    c = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, connect_timeout=15)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db():
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(727274)")  # на случай нескольких воркеров
        cur.execute(SCHEMA)


def _adapt(v):
    return Json(v) if isinstance(v, (list, dict)) else v


def _out(table, row):
    if row is None:
        return None
    d = dict(row)
    for k, v in list(d.items()):
        if isinstance(v, (datetime.datetime, datetime.date)):
            d[k] = v.isoformat()
    if table == "products":
        d["desc"] = d.pop("description")
    return d


def _cols(table, data):
    m = COLUMN_MAP.get(table, {})
    return {m.get(k, k): _adapt(v) for k, v in data.items()}


def fetch(table, where="", params=(), order="id"):
    sql = f"SELECT * FROM {table} {where} ORDER BY {order}"
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return [_out(table, r) for r in cur.fetchall()]


def get(table, row_id):
    with conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} WHERE id = %s", (row_id,))
        return _out(table, cur.fetchone())


def insert(table, data):
    cols = _cols(table, data)
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    with conn() as c, c.cursor() as cur:
        cur.execute(f"INSERT INTO {table} ({names}) VALUES ({marks}) RETURNING *", list(cols.values()))
        return _out(table, cur.fetchone())


def update(table, row_id, data):
    cols = _cols(table, data)
    if not cols:
        return get(table, row_id)
    sets = ", ".join(f"{k} = %s" for k in cols)
    with conn() as c, c.cursor() as cur:
        cur.execute(f"UPDATE {table} SET {sets} WHERE id = %s RETURNING *", list(cols.values()) + [row_id])
        return _out(table, cur.fetchone())


def delete(table, row_id):
    with conn() as c, c.cursor() as cur:
        cur.execute(f"DELETE FROM {table} WHERE id = %s", (row_id,))
        return cur.rowcount > 0


def add_message(table, row_id, message, mark_status=None):
    """Дописывает сообщение в messages. mark_status: (из_статуса, в_статус) либо строка."""
    with conn() as c, c.cursor() as cur:
        if isinstance(mark_status, tuple):
            cur.execute(
                f"UPDATE {table} SET messages = messages || %s::jsonb, "
                f"status = CASE WHEN status = %s THEN %s ELSE status END WHERE id = %s RETURNING *",
                (Json([message]), mark_status[0], mark_status[1], row_id),
            )
        elif isinstance(mark_status, str):
            cur.execute(
                f"UPDATE {table} SET messages = messages || %s::jsonb, status = %s WHERE id = %s RETURNING *",
                (Json([message]), mark_status, row_id),
            )
        else:
            cur.execute(
                f"UPDATE {table} SET messages = messages || %s::jsonb WHERE id = %s RETURNING *",
                (Json([message]), row_id),
            )
        return _out(table, cur.fetchone())


def promo_by_code(code):
    rows = fetch("promos", "WHERE code = %s", (code,))
    return rows[0] if rows else None


def promo_increment(promo_id):
    with conn() as c, c.cursor() as cur:
        cur.execute("UPDATE promos SET used_count = used_count + 1 WHERE id = %s", (promo_id,))


def customer_used_promo(code, chat_id):
    """Применял ли покупатель промокод (отменённые заказы не считаются)."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM orders WHERE promo_code = %s AND customer_chat_id = %s "
            "AND status <> 'cancelled' LIMIT 1",
            (code, chat_id),
        )
        return cur.fetchone() is not None
