"""
test_pipeline.py — Test suite for the ETL pipeline.

Covers:
  - Date and price parsers
  - Country / category / status normalisation
  - Quarantine logic (bad rows rejected, not silently dropped)
  - Idempotency (running twice produces same row counts)
  - Incremental loading (new file picked up; old file skipped)
  - Quality gates (pass on clean data, raise on bad data)
"""
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

# ── import modules under test ─────────────────────────────────────────────────
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
    check_rejects_logged,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Parser tests
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Normalisation map tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalisationMaps:
    def test_country_usa_variants(self):
        for variant in ["u.s.a", "usa", "us", "united states"]:
            assert COUNTRY_MAP[variant] == "United States"

    def test_country_canada_variants(self):
        for variant in ["ca", "canada"]:
            assert COUNTRY_MAP[variant] == "Canada"

    def test_category_case_insensitive_lookup(self):
        assert CATEGORY_MAP["apparel"] == "Apparel"
        assert CATEGORY_MAP["electronics"] == "Electronics"

    def test_status_normalisation(self):
        assert STATUS_MAP["pending"]   == "Pending"
        assert STATUS_MAP["shipped"]   == "Shipped"
        assert STATUS_MAP["delivered"] == "Delivered"
        assert STATUS_MAP["cancelled"] == "Cancelled"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Quality gate unit tests (in-memory SQLite)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_warehouse() -> sqlite3.Connection:
    """Create an in-memory warehouse with valid data."""
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


def _make_rejects() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE rej_customers (customer_id TEXT, reason TEXT, rejected_at TEXT);
        INSERT INTO rej_customers VALUES ('C999','duplicate','2023-01-01');
    """)
    return conn


class TestQualityGates:
    def test_min_rows_passes(self):
        conn = _make_warehouse()
        check_min_rows(conn, "dim_customers", 1)  # should not raise

    def test_min_rows_fails(self):
        conn = _make_warehouse()
        with pytest.raises(QualityGateError):
            check_min_rows(conn, "dim_customers", 999)

    def test_no_null_pk_passes(self):
        conn = _make_warehouse()
        check_no_null_primary_keys(conn)  # should not raise

    def test_no_null_pk_fails(self):
        conn = _make_warehouse()
        conn.execute("INSERT INTO dim_customers VALUES (NULL,'Bob','b@c.com','Canada','2023-01-01')")
        with pytest.raises(QualityGateError):
            check_no_null_primary_keys(conn)

    def test_no_orphan_fk_passes(self):
        conn = _make_warehouse()
        check_no_orphan_foreign_keys(conn)

    def test_no_orphan_fk_fails(self):
        conn = _make_warehouse()
        conn.execute("INSERT INTO fact_orders VALUES ('O002','C999','P001',1,'2023-06-01',9.99,'Pending')")
        with pytest.raises(QualityGateError):
            check_no_orphan_foreign_keys(conn)

    def test_amounts_positive_passes(self):
        conn = _make_warehouse()
        check_amounts_positive(conn)

    def test_amounts_positive_fails(self):
        conn = _make_warehouse()
        conn.execute("INSERT INTO fact_orders VALUES ('O003','C001','P001',1,'2023-06-01',-5,'Pending')")
        with pytest.raises(QualityGateError):
            check_amounts_positive(conn)

    def test_quantity_positive_passes(self):
        conn = _make_warehouse()
        check_quantity_positive(conn)

    def test_quantity_positive_fails(self):
        conn = _make_warehouse()
        conn.execute("INSERT INTO fact_orders VALUES ('O004','C001','P001',0,'2023-06-01',9.99,'Pending')")
        with pytest.raises(QualityGateError):
            check_quantity_positive(conn)

    def test_no_duplicate_order_ids_passes(self):
        conn = _make_warehouse()
        check_no_duplicate_order_ids(conn)

    def test_status_values_passes(self):
        conn = _make_warehouse()
        check_status_values(conn)

    def test_status_values_fails(self):
        conn = _make_warehouse()
        conn.execute("INSERT INTO fact_orders VALUES ('O005','C001','P001',1,'2023-06-01',9.99,'UNKNOWN')")
        with pytest.raises(QualityGateError):
            check_status_values(conn)

    def test_rejects_logged_passes(self):
        rconn = _make_rejects()
        check_rejects_logged(rconn)

    def test_rejects_logged_fails_on_empty_db(self):
        rconn = sqlite3.connect(":memory:")
        with pytest.raises(QualityGateError):
            check_rejects_logged(rconn)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Integration tests — idempotency & incremental loading
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def tmp_project(tmp_path):
    """
    Stand up a minimal project tree with real raw data files
    and patch config paths to point at tmp_path.
    """
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True)

    # customers
    (raw_dir / "customers.csv").write_text(
        "customer_id,name,email,country,signup_date\n"
        "C001,Alice,alice@example.com,United States,2023-01-01\n"
        "C002,Bob,bob@example.com,Canada,2023-02-01\n"
    )
    # products
    (raw_dir / "products.csv").write_text(
        "product_id,product_name,category,price\n"
        "P001,Widget,electronics,9.99\n"
        "P002,Book,books,14.99\n"
    )
    # order file 1
    (raw_dir / "orders_2023-07-01.csv").write_text(
        "order_id,customer_id,product_id,quantity,order_date,amount,status\n"
        "O001,C001,P001,2,2023-07-01,19.98,Shipped\n"
        "O002,C002,P002,1,2023-07-01,14.99,Pending\n"
    )
    # web events
    events = [
        {"user_id": "C001", "event": "view", "ts_ms": 1688400000000, "page": "/home"},
    ]
    (raw_dir / "web_events.json").write_text(json.dumps(events))

    return tmp_path


def _patch_config(tmp_path):
    """Return a dict of patches to redirect config paths to tmp_path."""
    import etl.config as cfg
    import etl.extract.extractors as ext
    import etl.transform.transformers as trn
    import etl.load.loader as ldr
    import etl.quality.gates as gts

    data_dir = tmp_path / "data"
    patches = {
        "etl.config.RAW_DIR":       tmp_path / "data" / "raw",
        "etl.config.STAGING_DB":    data_dir / "staging"  / "staging.db",
        "etl.config.WAREHOUSE_DB":  data_dir / "warehouse"/ "warehouse.db",
        "etl.config.REJECTS_DB":    data_dir / "rejects"  / "rejects.db",
        "etl.config.RUN_HISTORY":   data_dir / "logs"     / "run_history.jsonl",
        "etl.config.LOGS_DIR":      data_dir / "logs",
        "etl.config.CUSTOMERS_FILE":tmp_path / "data" / "raw" / "customers.csv",
        "etl.config.PRODUCTS_FILE": tmp_path / "data" / "raw" / "products.csv",
        "etl.config.WEB_EVENTS_FILE":tmp_path/"data"/"raw"/"web_events.json",
    }
    return patches


class TestIdempotency:
    def test_load_twice_no_duplicates(self, tmp_project):
        patches = _patch_config(tmp_project)
        with patch.multiple("etl.config", **{k.split("etl.config.")[1]: v
                                              for k, v in patches.items()}), \
             patch.multiple("etl.extract.extractors",
                            STAGING_DB=patches["etl.config.STAGING_DB"],
                            RAW_DIR=patches["etl.config.RAW_DIR"],
                            CUSTOMERS_FILE=patches["etl.config.CUSTOMERS_FILE"],
                            PRODUCTS_FILE=patches["etl.config.PRODUCTS_FILE"],
                            WEB_EVENTS_FILE=patches["etl.config.WEB_EVENTS_FILE"]), \
             patch.multiple("etl.transform.transformers",
                            STAGING_DB=patches["etl.config.STAGING_DB"],
                            REJECTS_DB=patches["etl.config.REJECTS_DB"]), \
             patch.multiple("etl.load.loader",
                            WAREHOUSE_DB=patches["etl.config.WAREHOUSE_DB"]), \
             patch.multiple("etl.quality.gates",
                            WAREHOUSE_DB=patches["etl.config.WAREHOUSE_DB"],
                            REJECTS_DB=patches["etl.config.REJECTS_DB"]):

            from etl.extract.extractors   import run_extract
            from etl.transform.transformers import run_transform
            from etl.load.loader          import run_load

            # Run 1
            run_extract(); clean = run_transform(); run_load(clean)
            # Run 2
            run_extract(); clean = run_transform(); run_load(clean)

            wconn = sqlite3.connect(patches["etl.config.WAREHOUSE_DB"])
            orders = wconn.execute("SELECT COUNT(*) FROM fact_orders").fetchone()[0]
            customers = wconn.execute("SELECT COUNT(*) FROM dim_customers").fetchone()[0]
            wconn.close()

            assert orders    == 2, f"Expected 2 orders, got {orders}"
            assert customers == 2, f"Expected 2 customers, got {customers}"


class TestIncrementalLoading:
    def test_new_file_picked_up(self, tmp_project):
        raw_dir = tmp_project / "data" / "raw"
        patches = _patch_config(tmp_project)

        with patch.multiple("etl.config", **{k.split("etl.config.")[1]: v
                                              for k, v in patches.items()}), \
             patch.multiple("etl.extract.extractors",
                            STAGING_DB=patches["etl.config.STAGING_DB"],
                            RAW_DIR=patches["etl.config.RAW_DIR"],
                            CUSTOMERS_FILE=patches["etl.config.CUSTOMERS_FILE"],
                            PRODUCTS_FILE=patches["etl.config.PRODUCTS_FILE"],
                            WEB_EVENTS_FILE=patches["etl.config.WEB_EVENTS_FILE"]):

            from etl.extract.extractors import run_extract

            # First run — picks up orders_2023-07-01.csv
            r1 = run_extract()
            assert "orders_2023-07-01.csv" in r1["orders"]["files_processed"]

            # Add a new order file
            (raw_dir / "orders_2023-07-02.csv").write_text(
                "order_id,customer_id,product_id,quantity,order_date,amount,status\n"
                "O003,C001,P001,1,2023-07-02,9.99,Delivered\n"
            )

            # Second run — should pick up only the new file
            r2 = run_extract()
            assert "orders_2023-07-02.csv" in r2["orders"]["files_processed"]
            assert "orders_2023-07-01.csv" not in r2["orders"]["files_processed"]

    def test_existing_file_skipped(self, tmp_project):
        patches = _patch_config(tmp_project)

        with patch.multiple("etl.config", **{k.split("etl.config.")[1]: v
                                              for k, v in patches.items()}), \
             patch.multiple("etl.extract.extractors",
                            STAGING_DB=patches["etl.config.STAGING_DB"],
                            RAW_DIR=patches["etl.config.RAW_DIR"],
                            CUSTOMERS_FILE=patches["etl.config.CUSTOMERS_FILE"],
                            PRODUCTS_FILE=patches["etl.config.PRODUCTS_FILE"],
                            WEB_EVENTS_FILE=patches["etl.config.WEB_EVENTS_FILE"]):

            from etl.extract.extractors import run_extract

            run_extract()           # loads the file
            r2 = run_extract()      # should skip it
            assert r2["orders"]["files_processed"] == []
