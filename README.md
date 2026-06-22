# ETL Pipeline Project

A production-style ETL pipeline that ingests messy e-commerce data from multiple sources (CSV and JSON), cleans and validates it, models it into a star-schema warehouse, and runs on a schedule with quality gates and structured logging.

---

## Quick start

```bash
# 1. Clone and enter the repo
git clone https://github.com/vimaladithyaasv-lang/etl_pipeline_project.git
cd etl_pipeline_project

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the full pipeline
python -m etl.orchestrate.pipeline

# 4. Generate the analytics report
python -m etl.analytics

# 5. Run tests
pytest tests/ -v
```

---

## Project structure

```
etl_pipeline_project/
├── data/
│   ├── raw/              # Source files (never modified)
│   │   ├── customers.csv
│   │   ├── products.csv
│   │   ├── orders_2023-07-01.csv
│   │   ├── orders_2023-07-02.csv
│   │   ├── orders_2023-07-03.csv
│   │   └── web_events.json
│   ├── staging/          # staging.db  (generated)
│   ├── warehouse/        # warehouse.db (generated)
│   ├── rejects/          # rejects.db  (generated)
│   └── logs/             # run_history.jsonl + per-run .log files (generated)
├── etl/
│   ├── config.py         # All paths and constants -- nothing hardcoded elsewhere
│   ├── extract/
│   │   └── extractors.py # CSV + JSON extractors; incremental order tracking
│   ├── transform/
│   │   └── transformers.py # Cleaning, normalisation, dedup, quarantine
│   ├── load/
│   │   └── loader.py     # Idempotent upserts into star schema
│   ├── quality/
│   │   └── gates.py      # 11 quality assertions -- halt on QualityGateError
│   ├── orchestrate/
│   │   └── pipeline.py   # Runs all stages; structured logging; run_history.jsonl
│   └── analytics.py      # Analytics queries + markdown report generator
├── tests/
│   └── test_pipeline.py  # 35 tests covering parsers, gates, idempotency, incremental
├── reports/
│   └── data_profile.md   # Week 1 data profiling report
├── schedule_pipeline.py  # Scheduler entry point (cron / Windows Task Scheduler)
└── requirements.txt
```

---

## Running on a schedule

**Linux / macOS (cron)** — run daily at 06:00:
```bash
crontab -e
# Add this line:
0 6 * * * cd /path/to/etl_pipeline_project && python schedule_pipeline.py
```

**Windows Task Scheduler:**
- Action: `python C:\path\to\etl_pipeline_project\schedule_pipeline.py`
- Start in: `C:\path\to\etl_pipeline_project`
- Schedule: Daily at 06:00

The script exits with code 0 on success and 1 on failure so the scheduler can detect and alert on failures.

---

## Incremental loading

Order files (`orders_YYYY-MM-DD.csv`) are loaded incrementally. Each processed filename is recorded in `staging._file_log`. Re-running the pipeline skips already-loaded files and only picks up new ones.

To force a full reprocess of all order files:
```bash
python -m etl.orchestrate.pipeline --reset-incremental
```

---

## Idempotency

The pipeline is safe to re-run at any time:

- **Warehouse:** all loads use `INSERT OR REPLACE` keyed on primary keys -- no duplicates accumulate.
- **Rejects:** cleared at the start of each transform run -- counts are identical across re-runs.

Proven by `TestIdempotency` in `tests/test_pipeline.py`.

---

## Quality gates (11 assertions)

Gates run between stages (pre-load emptiness check) and after load (full assertions). Any failure raises `QualityGateError` and halts the pipeline immediately.

| Gate | What it checks |
|---|---|
| `min_rows_customers` | At least MIN_CUSTOMERS rows in dim_customers |
| `min_rows_products` | At least MIN_PRODUCTS rows in dim_products |
| `min_rows_orders` | At least MIN_ORDERS rows in fact_orders |
| `no_null_primary_keys` | No NULL PKs in any dimension or fact table |
| `no_orphan_foreign_keys` | All FKs in fact tables resolve to a dimension row |
| `amounts_positive` | All order amounts > 0 |
| `quantity_positive` | All order quantities >= 1 |
| `no_duplicate_order_ids` | No duplicate order_ids in fact_orders |
| `status_values` | Only known status values (Pending/Shipped/Delivered/Cancelled) |
| `totals_reconcile` | Total revenue in fact_orders > 0 |
| `rejects_have_rows` | Rejects DB has rows -- confirms bad data was caught, not lost |

Thresholds (MIN_CUSTOMERS, MIN_PRODUCTS, MIN_ORDERS) are set in `etl/config.py`.

---

## Data quality issues handled

See `reports/data_profile.md` for the full profiling report. Summary:

| Issue | Handled in |
|---|---|
| Mixed date formats (3 variants) | `_parse_date()` in transformers.py |
| Currency strings (`$98.97`) | `_parse_price()` in transformers.py |
| 13 country variants -> 5 canonical | `COUNTRY_MAP` in config.py |
| 11 category variants -> 5 canonical | `CATEGORY_MAP` in config.py |
| 7 status variants -> 4 canonical | `STATUS_MAP` in config.py |
| Uppercase / padded emails | `.str.lower().str.strip()` |
| Malformed emails | Regex quarantine |
| Duplicate customer_id / email | Dedup + quarantine |
| Duplicate product_id | Dedup + quarantine |
| Duplicate order_id | Dedup + quarantine |
| Invalid quantity (0 / negative) | Quarantine |
| Missing amount | Recomputed from price x quantity |
| Unknown customer / product FKs | Quarantine |
| Unknown user FKs (web events) | Quarantine |
| Epoch ms timestamps | Converted to ISO datetime |

All rejected rows are written to `rejects.db` with a reason column -- nothing is silently dropped.

---

## Tests (35)

```bash
pytest tests/ -v
```

Covers: date/price parsers, normalisation maps, all 11 quality gates (pass + fail cases), warehouse idempotency, rejects idempotency, incremental loading (new file picked up, old file skipped).
