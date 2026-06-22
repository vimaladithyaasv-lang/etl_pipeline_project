"""
transformers.py — Transform layer.

Responsibilities:
  - Standardise formats (dates -> ISO, prices -> float, categories/countries/
    statuses -> canonical values).
  - Deduplicate on primary keys.
  - Quarantine bad rows into rejects.db with a reason -- never silently drop.
  - Rejects are idempotent: tables are cleared at the start of each run,
    so row counts never drift across re-runs.
"""
import logging
import re
import sqlite3
from datetime import datetime, timezone

import pandas as pd

from etl.config import (
    STAGING_DB, REJECTS_DB,
    COUNTRY_MAP, CATEGORY_MAP, STATUS_MAP,
)

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# -- helpers ------------------------------------------------------------------

def _staging_conn() -> sqlite3.Connection:
    return sqlite3.connect(STAGING_DB)


def _rejects_conn() -> sqlite3.Connection:
    """
    Open rejects.db, create tables if needed, and CLEAR them so re-runs
    produce identical row counts (idempotency fix for bug #1).
    """
    REJECTS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(REJECTS_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rej_customers (
            customer_id TEXT, name TEXT, email TEXT, country TEXT,
            signup_date TEXT, reason TEXT, rejected_at TEXT
        );
        CREATE TABLE IF NOT EXISTS rej_products (
            product_id TEXT, product_name TEXT, category TEXT,
            price TEXT, reason TEXT, rejected_at TEXT
        );
        CREATE TABLE IF NOT EXISTS rej_orders (
            order_id TEXT, customer_id TEXT, product_id TEXT,
            quantity TEXT, order_date TEXT, amount TEXT, status TEXT,
            _source_file TEXT, reason TEXT, rejected_at TEXT
        );
        CREATE TABLE IF NOT EXISTS rej_web_events (
            user_id TEXT, event TEXT, event_ts TEXT, page TEXT,
            reason TEXT, rejected_at TEXT
        );
        -- Clear all rejects so each run is idempotent
        DELETE FROM rej_customers;
        DELETE FROM rej_products;
        DELETE FROM rej_orders;
        DELETE FROM rej_web_events;
    """)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quarantine(rconn: sqlite3.Connection, table: str,
                rows: pd.DataFrame, reason: str) -> None:
    if rows.empty:
        return
    rows = rows.copy()
    rows["reason"]      = reason
    rows["rejected_at"] = _now()
    rows.to_sql(table, rconn, if_exists="append", index=False)
    rconn.commit()
    log.warning(f"quarantine -> {table}: {len(rows)} rows | {reason}")


def _parse_date(val: str) -> str | None:
    """Try multiple date formats; return ISO string or None."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(val).strip(), fmt).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    return None


def _parse_price(val) -> float | None:
    """Strip $ and parse to float."""
    try:
        return float(str(val).replace("$", "").strip())
    except (ValueError, TypeError):
        return None


# -- per-source transforms ----------------------------------------------------

def transform_customers(sconn: sqlite3.Connection,
                        rconn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql("SELECT * FROM stg_customers", sconn)
    df.columns = df.columns.str.strip()

    for col in df.select_dtypes("object").columns:
        df[col] = df[col].str.strip()

    df["email"] = df["email"].str.lower()

    df["country"] = df["country"].str.strip().str.lower().map(
        lambda x: COUNTRY_MAP.get(x, None)
    )

    df["signup_date"] = df["signup_date"].apply(_parse_date)

    # Quarantine: malformed email
    bad_email = df[~df["email"].apply(
        lambda e: bool(EMAIL_RE.match(str(e))) if pd.notna(e) else False
    )]
    _quarantine(rconn, "rej_customers", bad_email, "malformed email")
    df = df[~df.index.isin(bad_email.index)]

    # Quarantine: unknown country
    bad_country = df[df["country"].isna()]
    _quarantine(rconn, "rej_customers", bad_country, "unknown country")
    df = df[~df.index.isin(bad_country.index)]

    # Quarantine: duplicate customer_id (keep first)
    dup_cid = df[df.duplicated("customer_id", keep="first")]
    _quarantine(rconn, "rej_customers", dup_cid, "duplicate customer_id")
    df = df.drop_duplicates("customer_id", keep="first")

    # Quarantine: duplicate email (keep first)
    dup_email = df[df.duplicated("email", keep="first")]
    _quarantine(rconn, "rej_customers", dup_email, "duplicate email")
    df = df.drop_duplicates("email", keep="first")

    log.info(f"transform_customers: {len(df)} clean rows")
    return df.reset_index(drop=True)


def transform_products(sconn: sqlite3.Connection,
                       rconn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql("SELECT * FROM stg_products", sconn)
    df.columns = df.columns.str.strip()

    for col in df.select_dtypes("object").columns:
        df[col] = df[col].str.strip()

    df["price"] = df["price"].apply(_parse_price)

    df["category"] = df["category"].str.strip().str.lower().map(
        lambda x: CATEGORY_MAP.get(x, None) if pd.notna(x) else None
    )

    # Quarantine: invalid price
    bad_price = df[df["price"].isna() | (df["price"] <= 0)]
    _quarantine(rconn, "rej_products", bad_price, "invalid price")
    df = df[~df.index.isin(bad_price.index)]

    # Quarantine: unrecognised category
    bad_cat = df[df["category"].isna()]
    _quarantine(rconn, "rej_products", bad_cat, "unrecognised category")
    df = df[~df.index.isin(bad_cat.index)]

    # Quarantine: duplicate product_id (keep first — fix #10: price_map uses same)
    dup = df[df.duplicated("product_id", keep="first")]
    _quarantine(rconn, "rej_products", dup, "duplicate product_id")
    df = df.drop_duplicates("product_id", keep="first")

    log.info(f"transform_products: {len(df)} clean rows")
    return df.reset_index(drop=True)


def transform_orders(sconn: sqlite3.Connection,
                     rconn: sqlite3.Connection,
                     valid_customer_ids: set,
                     valid_product_ids: set,
                     clean_products: pd.DataFrame) -> pd.DataFrame:
    df = pd.read_sql("SELECT * FROM stg_orders", sconn)
    df.columns = df.columns.str.strip()

    for col in df.select_dtypes("object").columns:
        df[col] = df[col].str.strip()

    df["status"] = df["status"].str.strip().str.lower().map(
        lambda x: STATUS_MAP.get(x, None)
    )

    df["order_date"] = df["order_date"].apply(_parse_date)
    df["quantity"]   = pd.to_numeric(df["quantity"], errors="coerce")
    df["amount"]     = pd.to_numeric(
        df["amount"].astype(str).str.replace("$", "", regex=False),
        errors="coerce"
    )

    # Quarantine: duplicate order_id
    dup = df[df.duplicated("order_id", keep="first")]
    _quarantine(rconn, "rej_orders", dup, "duplicate order_id")
    df = df.drop_duplicates("order_id", keep="first")

    # Quarantine: invalid quantity
    bad_qty = df[df["quantity"].isna() | (df["quantity"] < 1)]
    _quarantine(rconn, "rej_orders", bad_qty, "invalid quantity")
    df = df[~df.index.isin(bad_qty.index)]

    # Quarantine: unknown customer_id
    bad_cust = df[~df["customer_id"].isin(valid_customer_ids)]
    _quarantine(rconn, "rej_orders", bad_cust, "unknown customer_id")
    df = df[~df.index.isin(bad_cust.index)]

    # Quarantine: unknown product_id
    bad_prod = df[~df["product_id"].isin(valid_product_ids)]
    _quarantine(rconn, "rej_orders", bad_prod, "unknown product_id")
    df = df[~df.index.isin(bad_prod.index)]

    # Quarantine: unknown status
    bad_status = df[df["status"].isna()]
    _quarantine(rconn, "rej_orders", bad_status, "unknown status")
    df = df[~df.index.isin(bad_status.index)]

    # Recompute missing amount using CLEAN deduplicated product prices (fix #10)
    price_map = clean_products.set_index("product_id")["price"].to_dict()
    missing_amt = df["amount"].isna()
    if missing_amt.any():
        df.loc[missing_amt, "amount"] = df.loc[missing_amt].apply(
            lambda r: price_map[r["product_id"]] * r["quantity"]
            if r["product_id"] in price_map else None,
            axis=1,
        )

    still_missing = df[df["amount"].isna()]
    _quarantine(rconn, "rej_orders", still_missing, "missing amount after recompute")
    df = df[~df.index.isin(still_missing.index)]

    df["quantity"] = df["quantity"].astype(int)
    df["amount"]   = df["amount"].round(2)

    log.info(f"transform_orders: {len(df)} clean rows")
    return df.reset_index(drop=True)


def transform_web_events(sconn: sqlite3.Connection,
                         rconn: sqlite3.Connection,
                         valid_customer_ids: set) -> pd.DataFrame:
    df = pd.read_sql("SELECT * FROM stg_web_events", sconn)
    df.columns = df.columns.str.strip()

    df["event_ts"] = pd.to_datetime(df["ts_ms"], unit="ms").dt.strftime("%Y-%m-%dT%H:%M:%S")
    df = df.drop(columns=["ts_ms"])

    bad = df[~df["user_id"].isin(valid_customer_ids)]
    _quarantine(rconn, "rej_web_events", bad, "unknown user_id")
    df = df[~df.index.isin(bad.index)]

    log.info(f"transform_web_events: {len(df)} clean rows")
    return df.reset_index(drop=True)


# -- entry point --------------------------------------------------------------

def run_transform() -> dict:
    sconn = _staging_conn()
    rconn = _rejects_conn()

    customers  = transform_customers(sconn, rconn)
    products   = transform_products(sconn, rconn)

    valid_cids = set(customers["customer_id"])
    valid_pids = set(products["product_id"])

    # Pass clean_products so price_map uses deduplicated, validated prices
    orders     = transform_orders(sconn, rconn, valid_cids, valid_pids, products)
    web_events = transform_web_events(sconn, rconn, valid_cids)

    sconn.close()
    rconn.close()

    return {
        "customers":  customers,
        "products":   products,
        "orders":     orders,
        "web_events": web_events,
    }
