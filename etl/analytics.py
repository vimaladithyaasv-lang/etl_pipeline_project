"""
analytics.py — Run analytics queries against the warehouse and write
               reports/analytics_report.md.
"""
import sqlite3
from pathlib import Path

import pandas as pd

from etl.config import WAREHOUSE_DB, REJECTS_DB, REPORTS_DIR


def _wconn():
    return sqlite3.connect(WAREHOUSE_DB)

def _rconn():
    return sqlite3.connect(REJECTS_DB)


def revenue_by_category(conn) -> pd.DataFrame:
    return pd.read_sql("""
        SELECT
            p.category,
            COUNT(o.order_id)   AS order_count,
            ROUND(SUM(o.amount), 2) AS total_revenue,
            ROUND(AVG(o.amount), 2) AS avg_order_value
        FROM fact_orders o
        JOIN dim_products p USING (product_id)
        GROUP BY p.category
        ORDER BY total_revenue DESC
    """, conn)


def top_customers(conn, n: int = 10) -> pd.DataFrame:
    return pd.read_sql(f"""
        SELECT
            c.customer_id,
            c.name,
            c.country,
            COUNT(o.order_id)       AS order_count,
            ROUND(SUM(o.amount), 2) AS lifetime_value,
            MAX(o.order_date)       AS last_order_date
        FROM fact_orders o
        JOIN dim_customers c USING (customer_id)
        GROUP BY c.customer_id
        ORDER BY lifetime_value DESC
        LIMIT {n}
    """, conn)


def daily_order_trend(conn) -> pd.DataFrame:
    return pd.read_sql("""
        SELECT
            order_date,
            COUNT(*)                AS orders,
            ROUND(SUM(amount), 2)   AS revenue,
            COUNT(DISTINCT customer_id) AS unique_customers
        FROM fact_orders
        GROUP BY order_date
        ORDER BY order_date
    """, conn)


def revenue_by_country(conn) -> pd.DataFrame:
    return pd.read_sql("""
        SELECT
            c.country,
            COUNT(o.order_id)       AS orders,
            ROUND(SUM(o.amount), 2) AS revenue
        FROM fact_orders o
        JOIN dim_customers c USING (customer_id)
        GROUP BY c.country
        ORDER BY revenue DESC
    """, conn)


def order_status_breakdown(conn) -> pd.DataFrame:
    return pd.read_sql("""
        SELECT
            status,
            COUNT(*)                AS count,
            ROUND(SUM(amount), 2)   AS value
        FROM fact_orders
        GROUP BY status
        ORDER BY count DESC
    """, conn)


def rejects_summary(rconn) -> pd.DataFrame:
    rows = []
    tables = [r[0] for r in rconn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    for t in tables:
        try:
            df = pd.read_sql(f"SELECT reason, COUNT(*) as rows FROM {t} GROUP BY reason", rconn)
            df.insert(0, "table", t)
            rows.append(df)
        except Exception:
            pass
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def generate_report() -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    wconn = _wconn()
    rconn = _rconn()

    sections = []

    sections.append("# ETL Pipeline — Analytics Report\n")

    sections.append("## Revenue by Category")
    sections.append(revenue_by_category(wconn).to_markdown(index=False))

    sections.append("\n## Top 10 Customers by Lifetime Value")
    sections.append(top_customers(wconn).to_markdown(index=False))

    sections.append("\n## Daily Order Trend")
    sections.append(daily_order_trend(wconn).to_markdown(index=False))

    sections.append("\n## Revenue by Country")
    sections.append(revenue_by_country(wconn).to_markdown(index=False))

    sections.append("\n## Order Status Breakdown")
    sections.append(order_status_breakdown(wconn).to_markdown(index=False))

    sections.append("\n## Rejects / Quarantine Summary")
    sections.append(rejects_summary(rconn).to_markdown(index=False))

    wconn.close()
    rconn.close()

    report_path = REPORTS_DIR / "analytics_report.md"
    report_path.write_text("\n".join(sections))
    print(f"Report written to {report_path}")
    return report_path


if __name__ == "__main__":
    generate_report()
