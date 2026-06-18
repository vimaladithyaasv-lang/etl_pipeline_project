# ETL Pipeline — E-Commerce Data Engineering

A production-style ETL pipeline that ingests messy e-commerce data from multiple formats (CSV, JSON), cleans and validates it, models it into a star-schema warehouse, and runs quality gates — with idempotent loads and incremental file processing.

## Quick start

```bash
# 1. Install dependencies
pip install pandas tabulate pytest

# 2. Run the full pipeline
cd etl_project
python -m etl.orchestrate.pipeline

# 3. Re-run safely (idempotent — no duplicates)
python -m etl.orchestrate.pipeline

# 4. Add a new day's orders file, then run incrementally
cp new_orders.csv data/raw/orders_2023-07-05.csv
python -m etl.orchestrate.pipeline   # only picks up the new file

# 5. Reset and reprocess all order files from scratch
python -m etl.orchestrate.pipeline --reset-incremental

# 6. Generate analytics report
python -m etl.analytics

# 7. Run tests
python -m pytest tests/ -v
```

## Project structure

```
etl_project/
├── data/
│   ├── raw/              # Untouched source files (customers, products, orders_*.csv, web_events.json)
│   ├── staging/          # staging.db — one table per source + _file_log for incremental tracking
│   ├── warehouse/        # warehouse.db — star schema (dim_customers, dim_products, fact_orders, fct_web_events)
│   ├── rejects/          # rejects.db — quarantined rows with reason + timestamp
│   └── logs/             # Per-run .log files + run_history.jsonl
├── etl/
│   ├── config.py         # All paths and constants (nothing hardcoded elsewhere)
│   ├── extract/
│   │   └── extractors.py # CSV + JSON extractors; incremental order-file tracking
│   ├── transform/
│   │   └── transformers.py # Cleaning, normalisation, dedup, quarantine
│   ├── load/
│   │   └── loader.py     # Idempotent upserts into star schema
│   ├── quality/
│   │   └── gates.py      # 8 quality assertions; raise QualityGateError to halt run
│   ├── orchestrate/
│   │   └── pipeline.py   # Runs extract→transform→load→quality, structured logging
│   └── analytics.py      # Analytics queries + Markdown report generator
├── reports/
│   └── analytics_report.md
└── tests/
    └── test_pipeline.py  # 29 tests covering parsers, idempotency, quality, incremental
```

## Architecture

### Layered design

| Layer | What it does |
|---|---|
| **Raw** | Source files, untouched |
| **Staging** | Parsed, type-cast; one table per source; `_file_log` tracks processed order files |
| **Clean** | Standardised, deduplicated, quarantine-filtered tables in staging |
| **Warehouse** | Star schema — `dim_customers`, `dim_products`, `fact_orders`, `fct_web_events` |
| **Rejects** | Every bad row, with a reason and timestamp — nothing silently dropped |
| **Quality gates** | 8 assertions that halt the run on violation |

### Star schema

```
dim_customers ←── fact_orders ──→ dim_products
                    (order_id PK,
                     customer_id FK,
                     product_id FK,
                     quantity, order_date,
                     amount, status)

dim_customers ←── fct_web_events
                    (user_id FK, event, event_ts, page)
```

## Data quality issues handled

| Issue | Source | Handling |
|---|---|---|
| Mixed date formats (`2023-06-18`, `03/28/2023`, `03-May-2023`) | orders, customers | Multi-format parser → ISO `YYYY-MM-DD` |
| Currency strings (`$98.97` vs `98.97`) | products | Strip `$`, parse float |
| Inconsistent country values (`u.s.a`, `USA`, `United States`, `CA`, `Canada`) | customers | Normalisation map → canonical names |
| Inconsistent category casing (`apparel`, `ELECTRONICS`, `books `) | products | Strip + lowercase + map |
| Inconsistent status casing (`pending`, `SHIPPED`, `delivered`) | orders | Lowercase + map |
| Whitespace-padded names | customers, products | `.str.strip()` on all string columns |
| UPPERCASE emails | customers | `.str.lower()` |
| Malformed email (`lakshmi.mullermail.com`) | customers | Regex validation → quarantine |
| Missing order amounts | orders | Recomputed from `price × quantity` |
| Duplicate `order_id` | orders | Keep first, quarantine rest |
| Duplicate `customer_id` | customers | Keep first, quarantine rest |
| Duplicate emails | customers | Keep first, quarantine rest (email is UNIQUE in warehouse) |
| Duplicate `product_id` | products | Keep first, quarantine rest |
| Invalid `product_id` (`P999`) | orders | Referential integrity check → quarantine |
| Invalid `customer_id` (`C9999`) | web_events | Referential integrity check → quarantine |
| Invalid/missing quantities | orders | Numeric parse + `> 0` check → quarantine |
| Unrecognised categories | products | Map lookup failure → quarantine |

## Idempotency

Every warehouse table uses `INSERT OR REPLACE` keyed on the primary key. Re-running the pipeline any number of times produces identical row counts. Verified by `TestIdempotency::test_load_twice_no_duplicates`.

## Incremental loading

Order files are tracked in `staging._file_log`. Each run only processes files not yet in the log. Adding `orders_2023-07-04.csv` and re-running picks up exactly that one file. Use `--reset-incremental` to reprocess all files from scratch.

## Quality gates (8 checks)

1. `check_min_rows` — warehouse tables must contain data
2. `check_no_null_primary_keys` — PKs never NULL
3. `check_no_orphan_foreign_keys` — no fact rows pointing to missing dims
4. `check_amounts_positive` — all amounts > 0
5. `check_quantity_positive` — all quantities ≥ 1
6. `check_no_duplicate_order_ids` — warehouse deduplication confirmed
7. `check_status_values` — only known statuses in warehouse
8. `check_rejects_logged` — rejects DB readable (nothing silently lost)

Any gate failure raises `QualityGateError` and halts the run immediately.
