"""
loader.py — Load layer.

Responsibilities:
  - Create the star-schema tables in warehouse.db if they don't exist.
  - Upsert (INSERT OR REPLACE) clean DataFrames so re-runs are idempotent.
  - Never truncate — only replace rows whose PK already exists.
"""
import logging
import sqlite3

import pandas as pd

from etl.config import WAREHOUSE_DB

log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    WAREHOUSE_DB.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(WAREHOUSE_DB)


def _init_warehouse(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dim_customers (
            customer_id  TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            email        TEXT NOT NULL UNIQUE,
            country      TEXT NOT NULL,
            signup_date  TEXT
        );

        CREATE TABLE IF NOT EXISTS dim_products (
            product_id   TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            category     TEXT NOT NULL,
            price        REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fact_orders (
            order_id    TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL REFERENCES dim_customers(customer_id),
            product_id  TEXT NOT NULL REFERENCES dim_products(product_id),
            quantity    INTEGER NOT NULL,
            order_date  TEXT NOT NULL,
            amount      REAL NOT NULL,
            status      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fct_web_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL REFERENCES dim_customers(customer_id),
            event       TEXT NOT NULL,
            event_ts    TEXT NOT NULL,
            page        TEXT
        );
    """)
    conn.commit()


def _upsert(conn: sqlite3.Connection, table: str,
            df: pd.DataFrame, pk: str) -> int:
    """
    Idempotent upsert: INSERT OR REPLACE keyed on pk.
    Returns number of rows written.
    """
    if df.empty:
        return 0

    cols        = ", ".join(df.columns)
    placeholders= ", ".join(["?"] * len(df.columns))
    sql = f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})"

    conn.executemany(sql, df.values.tolist())
    conn.commit()
    log.info(f"load → {table}: {len(df)} rows upserted")
    return len(df)


# ── public loaders ────────────────────────────────────────────────────────────

def load_customers(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    cols = ["customer_id", "name", "email", "country", "signup_date"]
    return _upsert(conn, "dim_customers", df[cols], "customer_id")


def load_products(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    cols = ["product_id", "product_name", "category", "price"]
    return _upsert(conn, "dim_products", df[cols], "product_id")


def load_orders(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    cols = ["order_id", "customer_id", "product_id",
            "quantity", "order_date", "amount", "status"]
    return _upsert(conn, "fact_orders", df[cols], "order_id")


def load_web_events(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """
    Web events have no natural PK so we clear and reload on each run
    (the source is always a full snapshot).
    """
    if df.empty:
        return 0
    conn.execute("DELETE FROM fct_web_events")
    cols = ["user_id", "event", "event_ts", "page"]
    df[cols].to_sql("fct_web_events", conn, if_exists="append", index=False)
    conn.commit()
    log.info(f"load → fct_web_events: {len(df)} rows")
    return len(df)


# ── entry point ───────────────────────────────────────────────────────────────

def run_load(clean: dict) -> dict:
    conn = _get_conn()
    _init_warehouse(conn)

    result = {
        "dim_customers":  load_customers(conn,   clean["customers"]),
        "dim_products":   load_products(conn,    clean["products"]),
        "fact_orders":    load_orders(conn,      clean["orders"]),
        "fct_web_events": load_web_events(conn,  clean["web_events"]),
    }
    conn.close()
    return result
