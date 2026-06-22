"""
test_pipeline.py -- Test suite for the ETL pipeline.

Covers:
  - Date and price parsers
  - Country / category / status normalisation
  - Quarantine logic (bad rows rejected, not silently dropped)
  - Idempotency: running twice produces identical row counts in both
    warehouse AND rejects (fix #1)
  - Incremental loading (new file picked up; old file skipped)
  - Quality gates (pass on clean data, raise on bad data)
"""
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from etl.transform.transformers import _parse_date, _parse_price
from etl.config import COUNTRY_MAP, CATEGORY_MAP, STATUS_MAP
from etl.quality.gates import (
    QualityGateError,
    check_min_rows,
    check_no_null_primary_keys,
    check_no_orphan_foreign_keys,
    check_amounts_positive,
    check_quantity_positive,
    check_no_duplicate_order_ids,
    check_status_values,
    check_totals_reconcile,
    check_rejects_have_rows,
)


# =============================================================================
# 1. Parser tests
# =============================================================================

class TestDateParser:
    def test_iso_format(self):
        assert _parse_date("2023-06-18") == "2023-06-18"

    def test_us_slash_format(self):
        assert _parse_date("03/28/2023") == "2023-03-28"

    def test_day_mon_year_format(self):
        assert _parse_date("03-May-2023") == "2023-05-03"

    def test_invalid_returns_none(self):
        assert _parse_date("not-a-date") is None

    def test_empty_string_returns_none(self):
        assert _parse_date("") is None


class TestPriceParser:
    def test_plain_number(self):
        assert _parse_price("30.05") == 30.05

    def test_dollar_sign(self):
        assert _parse_price("$98.97") == 98.97

    def test_integer_string(self):
        assert _parse_price("100") == 100.0

    def test_invalid_returns_none(self):
        assert _parse_price("free") is None

    def test_none_input(self):
        assert _parse_price(None) is None


# =============================================================================
# 2. Normalisation maps
# =============================================================================

class TestNormalisationMaps:
    def test_country_usa_variants(self):
        for v in ["u.s.a", "usa", "us", "united states"]:
            assert COUNTRY_MAP[v] == "United States"

    def test_country_canada_variants(self):
        for v in ["ca", "canada"]:
            assert COUNTRY_MAP[v] == "Canada"

    def test_category_lookup(self):
        assert CATEGORY_MAP["apparel"]     == "Apparel"
        assert CATEGORY_MAP["electronics"] == "Electronics"

    def test_status_normalisation(self):
        assert STATUS_MAP["pending"]   == "Pending"
        assert STATUS_MAP["shipped"]   == "Shipped"
        assert STATUS_MAP["delivered"] == "Delivered"
        assert STATUS_MAP["cancelled"] == "Cancelled"


# =============================================================================
# 3. Quality gate unit tests (in-memory SQLite)
# =============================================================================

def _make_warehouse() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE dim_customers (
            customer_id TEXT PRIMARY KEY, name TEXT, email TEXT,
            country TEXT, signup_date TEXT
        );
        CREATE TABLE dim_products (
            product_id TEXT PRIMARY KEY, product_name TEXT,
            category TEXT, price REAL
        );
        CREATE TABLE fact_orders (
            order_id TEXT PRIMARY KEY, customer_id TEXT, product_id TEXT,
            quantity INTEGER, order_date TEXT, amount REAL, status TEXT
        );
        CREATE TABLE fct_web_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, event TEXT, event_ts TEXT, page TEXT
        );
        INSERT INTO dim_customers VALUES ('C001','Alice','a@b.com','United States','2023-01-01');
        INSERT INTO dim_products  VALUES ('P001','Widget','Electronics',9.99);
        INSERT INTO fact_orders   VALUES ('O001','C001','P001',2,'2023-06-01',19.98,'Shipped');
        INSERT INTO fct_web_events(user_id,event,event_ts,page)
            VALUES ('C001','view','2023-06-01T10:00:00','/home');
    """)
    return conn


def _make_rejects(with_rows: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE rej_customers (customer_id TEXT, reason TEXT, rejected_at TEXT);
        CREATE TABLE rej_products  (product_id  TEXT, reason TEXT, rejected_at TEXT);
        CREATE TABLE rej_orders    (order_id    TEXT, reason TEXT, rejected_at TEXT);
        CREATE TABLE rej_web_events(user_id     TEXT, reason TEXT, rejected_at TEXT);
    """)
    if with_rows:
        conn.execute("INSERT INTO rej_customers VALUES ('C999','duplicate','2023-01-01')")
        conn.commit()
    return conn


class TestQualityGates:
    def test_min_rows_passes(self):
        check_min_rows(_make_warehouse(), "dim_customers", 1)

    def test_min_rows_fails(self):
        with pytest.raises(QualityGateError):
            check_min_rows(_make_warehouse(), "dim_customers", 999)

    def test_no_null_pk_passes(self):
        check_no_null_primary_keys(_make_warehouse())

    def test_no_null_pk_fails(self):
        conn = _make_warehouse()
        conn.execute("INSERT INTO dim_customers VALUES (NULL,'Bob','b@c.com','Canada','2023-01-01')")
        with pytest.raises(QualityGateError):
            check_no_null_primary_keys(conn)

    def test_no_orphan_fk_passes(self):
        check_no_orphan_foreign_keys(_make_warehouse())

    def test_no_orphan_fk_fails(self):
        conn = _make_warehouse()
        conn.execute("INSERT INTO fact_orders VALUES ('O002','C999','P001',1,'2023-06-01',9.99,'Pending')")
        with pytest.raises(QualityGateError):
            check_no_orphan_foreign_keys(conn)

    def test_amounts_positive_passes(self):
        check_amounts_positive(_make_warehouse())

    def test_amounts_positive_fails(self):
        conn = _make_warehouse()
        conn.execute("INSERT INTO fact_orders VALUES ('O003','C001','P001',1,'2023-06-01',-5,'Pending')")
        with pytest.raises(QualityGateError):
            check_amounts_positive(conn)

    def test_quantity_positive_passes(self):
        check_quantity_positive(_make_warehouse())

    def test_quantity_positive_fails(self):
        conn = _make_warehouse()
        conn.execute("INSERT INTO fact_orders VALUES ('O004','C001','P001',0,'2023-06-01',9.99,'Pending')")
        with pytest.raises(QualityGateError):
            check_quantity_positive(conn)

    def test_no_duplicate_order_ids_passes(self):
        check_no_duplicate_order_ids(_make_warehouse())

    def test_status_values_passes(self):
        check_status_values(_make_warehouse())

    def test_status_values_fails(self):
        conn = _make_warehouse()
        conn.execute("INSERT INTO fact_orders VALUES ('O005','C001','P001',1,'2023-06-01',9.99,'UNKNOWN')")
        with pytest.raises(QualityGateError):
            check_status_values(conn)

    def test_totals_reconcile_passes(self):
        check_totals_reconcile(_make_warehouse())

    def test_totals_reconcile_fails_on_empty(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE fact_orders (
            order_id TEXT, customer_id TEXT, product_id TEXT,
            quantity INTEGER, order_date TEXT, amount REAL, status TEXT)""")
        with pytest.raises(QualityGateError):
            check_totals_reconcile(conn)

    def test_rejects_have_rows_passes(self):
        check_rejects_have_rows(_make_rejects(with_rows=True))

    def test_rejects_have_rows_fails_when_empty(self):
        with pytest.raises(QualityGateError):
            check_rejects_have_rows(_make_rejects(with_rows=False))


# =============================================================================
# 4. Integration tests -- idempotency & incremental loading
# =============================================================================

@pytest.fixture()
def tmp_project(tmp_path):
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True)

    (raw_dir / "customers.csv").write_text(
        "customer_id,name,email,country,signup_date\n"
        "C001,Alice,alice@example.com,United States,2023-01-01\n"
        "C002,Bob,bob@example.com,Canada,2023-02-01\n"
    )
    (raw_dir / "products.csv").write_text(
        "product_id,product_name,category,price\n"
        "P001,Widget,electronics,9.99\n"
        "P002,Book,books,14.99\n"
    )
    (raw_dir / "orders_2023-07-01.csv").write_text(
        "order_id,customer_id,product_id,quantity,order_date,amount,status\n"
        "O001,C001,P001,2,2023-07-01,19.98,Shipped\n"
        "O002,C002,P002,1,2023-07-01,14.99,Pending\n"
    )
    events = [{"user_id": "C001", "event": "view", "ts_ms": 1688400000000, "page": "/home"}]
    (raw_dir / "web_events.json").write_text(json.dumps(events))
    return tmp_path


def _cfg(tmp_path):
    d = tmp_path / "data"
    return {
        "RAW_DIR":        tmp_path / "data" / "raw",
        "STAGING_DB":     d / "staging"   / "staging.db",
        "WAREHOUSE_DB":   d / "warehouse" / "warehouse.db",
        "REJECTS_DB":     d / "rejects"   / "rejects.db",
        "RUN_HISTORY":    d / "logs"      / "run_history.jsonl",
        "LOGS_DIR":       d / "logs",
        "CUSTOMERS_FILE": tmp_path / "data" / "raw" / "customers.csv",
        "PRODUCTS_FILE":  tmp_path / "data" / "raw" / "products.csv",
        "WEB_EVENTS_FILE":tmp_path / "data" / "raw" / "web_events.json",
    }


def _run_once(cfg):
    with patch.multiple("etl.config",        **cfg), \
         patch.multiple("etl.extract.extractors",
                        STAGING_DB=cfg["STAGING_DB"],
                        RAW_DIR=cfg["RAW_DIR"],
                        CUSTOMERS_FILE=cfg["CUSTOMERS_FILE"],
                        PRODUCTS_FILE=cfg["PRODUCTS_FILE"],
                        WEB_EVENTS_FILE=cfg["WEB_EVENTS_FILE"]), \
         patch.multiple("etl.transform.transformers",
                        STAGING_DB=cfg["STAGING_DB"],
                        REJECTS_DB=cfg["REJECTS_DB"]), \
         patch.multiple("etl.load.loader",
                        WAREHOUSE_DB=cfg["WAREHOUSE_DB"]):
        from etl.extract.extractors     import run_extract
        from etl.transform.transformers import run_transform
        from etl.load.loader            import run_load
        run_extract()
        clean = run_transform()
        run_load(clean)


class TestIdempotency:
    def test_warehouse_no_duplicates_on_second_run(self, tmp_project):
        cfg = _cfg(tmp_project)
        _run_once(cfg)
        _run_once(cfg)
        conn = sqlite3.connect(cfg["WAREHOUSE_DB"])
        assert conn.execute("SELECT COUNT(*) FROM fact_orders").fetchone()[0]    == 2
        assert conn.execute("SELECT COUNT(*) FROM dim_customers").fetchone()[0]  == 2
        conn.close()

    def test_rejects_identical_on_second_run(self, tmp_project):
        """Rejects must not grow on re-run (idempotency fix #1)."""
        cfg = _cfg(tmp_project)
        _run_once(cfg)
        r1 = sqlite3.connect(cfg["REJECTS_DB"]).execute(
            "SELECT COUNT(*) FROM rej_customers").fetchone()[0]
        _run_once(cfg)
        r2 = sqlite3.connect(cfg["REJECTS_DB"]).execute(
            "SELECT COUNT(*) FROM rej_customers").fetchone()[0]
        assert r1 == r2, f"Rejects grew from {r1} to {r2} on second run"


class TestIncrementalLoading:
    def test_new_file_picked_up(self, tmp_project):
        cfg = _cfg(tmp_project)
        raw_dir = tmp_project / "data" / "raw"
        with patch.multiple("etl.config", **cfg), \
             patch.multiple("etl.extract.extractors",
                            STAGING_DB=cfg["STAGING_DB"],
                            RAW_DIR=cfg["RAW_DIR"],
                            CUSTOMERS_FILE=cfg["CUSTOMERS_FILE"],
                            PRODUCTS_FILE=cfg["PRODUCTS_FILE"],
                            WEB_EVENTS_FILE=cfg["WEB_EVENTS_FILE"]):
            from etl.extract.extractors import run_extract
            r1 = run_extract()
            assert "orders_2023-07-01.csv" in r1["orders"]["files_processed"]

            (raw_dir / "orders_2023-07-02.csv").write_text(
                "order_id,customer_id,product_id,quantity,order_date,amount,status\n"
                "O003,C001,P001,1,2023-07-02,9.99,Delivered\n"
            )
            r2 = run_extract()
            assert "orders_2023-07-02.csv" in  r2["orders"]["files_processed"]
            assert "orders_2023-07-01.csv" not in r2["orders"]["files_processed"]

    def test_existing_file_skipped(self, tmp_project):
        cfg = _cfg(tmp_project)
        with patch.multiple("etl.config", **cfg), \
             patch.multiple("etl.extract.extractors",
                            STAGING_DB=cfg["STAGING_DB"],
                            RAW_DIR=cfg["RAW_DIR"],
                            CUSTOMERS_FILE=cfg["CUSTOMERS_FILE"],
                            PRODUCTS_FILE=cfg["PRODUCTS_FILE"],
                            WEB_EVENTS_FILE=cfg["WEB_EVENTS_FILE"]):
            from etl.extract.extractors import run_extract
            run_extract()
            r2 = run_extract()
            assert r2["orders"]["files_processed"] == []
