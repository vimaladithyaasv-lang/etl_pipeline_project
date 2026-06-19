"""
extractors.py — Extract layer.

Responsibilities:
  - Load customers.csv, products.csv, web_events.json into staging tables.
  - Load order files incrementally: only files not yet in staging._file_log.
  - Never modify raw files.
"""
import json
import logging
import sqlite3
from pathlib import Path

import pandas as pd

from etl.config import (
    CUSTOMERS_FILE, PRODUCTS_FILE, WEB_EVENTS_FILE,
    RAW_DIR, ORDERS_GLOB, STAGING_DB,
)

log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    STAGING_DB.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(STAGING_DB)


def _init_staging(conn: sqlite3.Connection) -> None:
    """Create staging tables and file-log if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stg_customers (
            customer_id  TEXT,
            name         TEXT,
            email        TEXT,
            country      TEXT,
            signup_date  TEXT
        );
        CREATE TABLE IF NOT EXISTS stg_products (
            product_id   TEXT,
            product_name TEXT,
            category     TEXT,
            price        TEXT
        );
        CREATE TABLE IF NOT EXISTS stg_orders (
            order_id    TEXT,
            customer_id TEXT,
            product_id  TEXT,
            quantity    TEXT,
            order_date  TEXT,
            amount      TEXT,
            status      TEXT,
            _source_file TEXT
        );
        CREATE TABLE IF NOT EXISTS stg_web_events (
            user_id  TEXT,
            event    TEXT,
            ts_ms    INTEGER,
            page     TEXT
        );
        CREATE TABLE IF NOT EXISTS _file_log (
            filename    TEXT PRIMARY KEY,
            loaded_at   TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()


# ── public extractors ─────────────────────────────────────────────────────────

def extract_customers(conn: sqlite3.Connection) -> int:
    """Load customers.csv into stg_customers (full reload each run)."""
    df = pd.read_csv(CUSTOMERS_FILE, dtype=str)
    conn.execute("DELETE FROM stg_customers")
    df.to_sql("stg_customers", conn, if_exists="append", index=False)
    conn.commit()
    log.info(f"extract_customers: {len(df)} rows loaded")
    return len(df)


def extract_products(conn: sqlite3.Connection) -> int:
    """Load products.csv into stg_products (full reload each run)."""
    df = pd.read_csv(PRODUCTS_FILE, dtype=str)
    conn.execute("DELETE FROM stg_products")
    df.to_sql("stg_products", conn, if_exists="append", index=False)
    conn.commit()
    log.info(f"extract_products: {len(df)} rows loaded")
    return len(df)


def extract_orders_incremental(conn: sqlite3.Connection) -> dict:
    """
    Load only order files not yet recorded in _file_log.
    Returns {"files_processed": [...], "rows_loaded": int}
    """
    order_files = sorted(RAW_DIR.glob(ORDERS_GLOB))
    already_loaded = {
        row[0] for row in conn.execute("SELECT filename FROM _file_log")
    }

    files_processed = []
    total_rows = 0

    for path in order_files:
        fname = path.name
        if fname in already_loaded:
            log.info(f"extract_orders: skipping {fname} (already loaded)")
            continue

        df = pd.read_csv(path, dtype=str)
        df["_source_file"] = fname
        df.to_sql("stg_orders", conn, if_exists="append", index=False)
        conn.execute(
            "INSERT OR IGNORE INTO _file_log (filename) VALUES (?)", (fname,)
        )
        conn.commit()
        log.info(f"extract_orders: loaded {len(df)} rows from {fname}")
        files_processed.append(fname)
        total_rows += len(df)

    return {"files_processed": files_processed, "rows_loaded": total_rows}


def extract_web_events(conn: sqlite3.Connection) -> int:
    """Load web_events.json into stg_web_events (full reload each run)."""
    with open(WEB_EVENTS_FILE) as f:
        records = json.load(f)
    df = pd.DataFrame(records)
    conn.execute("DELETE FROM stg_web_events")
    df.to_sql("stg_web_events", conn, if_exists="append", index=False)
    conn.commit()
    log.info(f"extract_web_events: {len(df)} rows loaded")
    return len(df)


def reset_incremental(conn: sqlite3.Connection) -> None:
    """Clear _file_log and stg_orders so all order files are reprocessed."""
    conn.execute("DELETE FROM _file_log")
    conn.execute("DELETE FROM stg_orders")
    conn.commit()
    log.info("reset_incremental: file log and stg_orders cleared")


# ── entry point ───────────────────────────────────────────────────────────────

def run_extract(reset: bool = False) -> dict:
    conn = _get_conn()
    _init_staging(conn)

    if reset:
        reset_incremental(conn)

    result = {
        "customers": extract_customers(conn),
        "products":  extract_products(conn),
        "orders":    extract_orders_incremental(conn),
        "web_events":extract_web_events(conn),
    }
    conn.close()
    return result
