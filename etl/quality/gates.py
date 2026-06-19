"""
gates.py — Quality gate layer.

8 assertions run after loading. Any failure raises QualityGateError
and halts the pipeline immediately.
"""
import logging
import sqlite3

from etl.config import WAREHOUSE_DB, REJECTS_DB, KNOWN_STATUSES

log = logging.getLogger(__name__)


class QualityGateError(Exception):
    """Raised when a data-quality assertion fails."""


def _wconn() -> sqlite3.Connection:
    return sqlite3.connect(WAREHOUSE_DB)


def _rconn() -> sqlite3.Connection:
    return sqlite3.connect(REJECTS_DB)


def _scalar(conn, sql):
    return conn.execute(sql).fetchone()[0]


# ── individual gates ──────────────────────────────────────────────────────────

def check_min_rows(conn: sqlite3.Connection,
                   table: str, minimum: int) -> None:
    n = _scalar(conn, f"SELECT COUNT(*) FROM {table}")
    if n < minimum:
        raise QualityGateError(
            f"{table} has {n} rows — expected at least {minimum}"
        )
    log.info(f"gate ✓ min_rows({table}): {n} >= {minimum}")


def check_no_null_primary_keys(conn: sqlite3.Connection) -> None:
    checks = [
        ("dim_customers", "customer_id"),
        ("dim_products",  "product_id"),
        ("fact_orders",   "order_id"),
    ]
    for table, pk in checks:
        n = _scalar(conn, f"SELECT COUNT(*) FROM {table} WHERE {pk} IS NULL")
        if n > 0:
            raise QualityGateError(f"{table}.{pk} has {n} NULL values")
        log.info(f"gate ✓ no_null_pk({table}.{pk})")


def check_no_orphan_foreign_keys(conn: sqlite3.Connection) -> None:
    checks = [
        ("fact_orders",    "customer_id", "dim_customers", "customer_id"),
        ("fact_orders",    "product_id",  "dim_products",  "product_id"),
        ("fct_web_events", "user_id",     "dim_customers", "customer_id"),
    ]
    for fact, fk, dim, pk in checks:
        n = _scalar(conn, f"""
            SELECT COUNT(*) FROM {fact}
            WHERE {fk} NOT IN (SELECT {pk} FROM {dim})
        """)
        if n > 0:
            raise QualityGateError(
                f"{fact}.{fk} has {n} orphan FK references"
            )
        log.info(f"gate ✓ no_orphan_fk({fact}.{fk})")


def check_amounts_positive(conn: sqlite3.Connection) -> None:
    n = _scalar(conn, "SELECT COUNT(*) FROM fact_orders WHERE amount <= 0")
    if n > 0:
        raise QualityGateError(f"fact_orders has {n} rows with amount <= 0")
    log.info("gate ✓ amounts_positive")


def check_quantity_positive(conn: sqlite3.Connection) -> None:
    n = _scalar(conn, "SELECT COUNT(*) FROM fact_orders WHERE quantity < 1")
    if n > 0:
        raise QualityGateError(f"fact_orders has {n} rows with quantity < 1")
    log.info("gate ✓ quantity_positive")


def check_no_duplicate_order_ids(conn: sqlite3.Connection) -> None:
    n = _scalar(conn, """
        SELECT COUNT(*) FROM (
            SELECT order_id FROM fact_orders
            GROUP BY order_id HAVING COUNT(*) > 1
        )
    """)
    if n > 0:
        raise QualityGateError(f"fact_orders has {n} duplicate order_ids")
    log.info("gate ✓ no_duplicate_order_ids")


def check_status_values(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT DISTINCT status FROM fact_orders"
    ).fetchall()
    bad = {r[0] for r in rows} - KNOWN_STATUSES
    if bad:
        raise QualityGateError(f"Unknown status values in warehouse: {bad}")
    log.info("gate ✓ status_values")


def check_rejects_logged(rconn: sqlite3.Connection) -> None:
    """Verify rejects DB is readable — nothing silently lost."""
    tables = [r[0] for r in rconn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    if not tables:
        raise QualityGateError("rejects.db is empty — possible silent data loss")
    log.info(f"gate ✓ rejects_logged (tables: {tables})")


# ── entry point ───────────────────────────────────────────────────────────────

def run_quality_gates() -> list[str]:
    """Run all 8 gates. Returns list of passing gate names."""
    wconn = _wconn()
    rconn = _rconn()
    passed = []

    try:
        check_min_rows(wconn, "dim_customers", 10);  passed.append("min_rows_customers")
        check_min_rows(wconn, "dim_products",  5);   passed.append("min_rows_products")
        check_min_rows(wconn, "fact_orders",   10);  passed.append("min_rows_orders")
        check_no_null_primary_keys(wconn);           passed.append("no_null_primary_keys")
        check_no_orphan_foreign_keys(wconn);         passed.append("no_orphan_foreign_keys")
        check_amounts_positive(wconn);               passed.append("amounts_positive")
        check_quantity_positive(wconn);              passed.append("quantity_positive")
        check_no_duplicate_order_ids(wconn);         passed.append("no_duplicate_order_ids")
        check_status_values(wconn);                  passed.append("status_values")
        check_rejects_logged(rconn);                 passed.append("rejects_logged")
    finally:
        wconn.close()
        rconn.close()

    return passed
