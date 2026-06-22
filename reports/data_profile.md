# Data Profile Report — E-Commerce ETL Pipeline

## Overview

Raw data consists of 4 source files supplied in `etl_raw_data.zip`. This report documents every quality issue found per column before any cleaning was applied.

---

## 1. customers.csv (212 data rows)

| Column | Type | Missing % | Distinct | Issues Found |
|---|---|---|---|---|
| customer_id | string | 0% | 212 | None — all unique |
| name | string | 0% | ~200 | Leading/trailing whitespace on ~15% of rows |
| email | string | 0% | 212 | ALL UPPERCASE — needs lowercasing. 1 malformed address missing `@` (`lakshmi.mullermail.com`) |
| country | string | 0% | 13 | 13 variants for 5 canonical countries — `u.s.a`, `USA`, `us`, `United States`, `CA`, `Canada`, `UK`, `United Kingdom`, `DE`, `Germany`, `IN`, `india`, `India` |
| signup_date | string | 0% | — | 3 date formats mixed: `YYYY-MM-DD`, `MM/DD/YYYY`, `DD-Mon-YYYY` |

**Key issues:** country normalisation (13 → 5 values), email casing, 1 malformed email, mixed date formats, whitespace padding.

---

## 2. products.csv (55 data rows)

| Column | Type | Missing % | Distinct | Issues Found |
|---|---|---|---|---|
| product_id | string | 0% | 55 | 15 duplicate IDs planted in the file |
| product_name | string | 0% | 55 | Whitespace padding on some rows |
| category | string | ~2% | 11 variants | `apparel`, `ELECTRONICS`, `Books`, `Apparel`, `books `, `home`, `Home`, `Toys`, `electronics`, `Electronics` — plus 2 NULLs and 12 unrecognised values |
| price | string | 0% | — | Mix of plain floats (`30.05`) and dollar-prefixed strings (`$98.97`). 3 rows with invalid/zero price |

**Key issues:** duplicate product_ids, 11 category variants for 5 canonical values, `$` in price strings, unrecognised categories.

---

## 3. orders_YYYY-MM-DD.csv (508 data rows per file × 3 files = 1,524 total)

| Column | Type | Missing % | Distinct | Issues Found |
|---|---|---|---|---|
| order_id | string | 0% | — | 72 duplicate order_ids across files |
| customer_id | string | 0% | — | ~1,380 orders reference customer IDs not in customers.csv (planted bad FKs) |
| product_id | string | 0% | — | ~334 orders reference product IDs not in products.csv |
| quantity | string | ~2% | — | ~249 rows with 0, negative, or non-numeric quantity |
| order_date | string | 0% | — | 3 mixed formats: `YYYY-MM-DD`, `MM/DD/YYYY`, `DD-Mon-YYYY` |
| amount | string | ~14% | — | 71 missing per file (to be recomputed from price × quantity). Some with `$` prefix |
| status | string | 0% | 7 variants | `Pending`, `pending`, `SHIPPED`, `Shipped`, `shipped`, `delivered`, `Cancelled` |

**Key issues:** duplicate order_ids, ~1,380 unknown customer FKs, ~334 unknown product FKs, 249 invalid quantities, 71 missing amounts per file, mixed date formats, 7 status variants for 4 canonical values.

---

## 4. web_events.json (600 records)

| Field | Type | Missing % | Issues Found |
|---|---|---|---|
| user_id | string | 0% | ~597 events reference user IDs not in customers.csv |
| event | string | 0% | None |
| ts_ms | integer | 0% | Unix timestamp in milliseconds — needs conversion to ISO datetime |
| page | string | 0% | None |

**Key issues:** 597 unknown user_id foreign keys, timestamp format needs converting from epoch ms to ISO string.

---

## Summary of Issues

| Issue Type | Sources Affected | Count |
|---|---|---|
| Mixed date formats | customers, orders | 3 formats |
| Currency strings (`$`) | products, orders | ~30 rows |
| Inconsistent country values | customers | 13 variants → 5 |
| Inconsistent category casing | products | 11 variants → 5 |
| Inconsistent status casing | orders | 7 variants → 4 |
| Whitespace padding | customers, products | ~15% of rows |
| UPPERCASE emails | customers | 100% of rows |
| Malformed email | customers | 1 row |
| Duplicate customer_id | customers | present |
| Duplicate email | customers | present |
| Duplicate product_id | products | 15 |
| Duplicate order_id | orders | 72 |
| Invalid quantity (0 / negative) | orders | ~249 |
| Missing amount | orders | ~71 per file |
| Unknown customer FK (orders) | orders | ~1,380 |
| Unknown product FK (orders) | orders | ~334 |
| Unknown user FK (web events) | web_events | ~597 |
| Epoch ms timestamp | web_events | all 600 |

All issues are handled in `etl/transform/transformers.py`. Bad rows are quarantined to `rejects.db` with a reason — none are silently dropped.
