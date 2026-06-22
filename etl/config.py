"""
config.py — single source of truth for all paths and constants.
Nothing is hardcoded anywhere else.
"""
from pathlib import Path

# ── Root paths ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR     = PROJECT_ROOT / "data"

RAW_DIR      = DATA_DIR / "raw"
STAGING_DIR  = DATA_DIR / "staging"
WAREHOUSE_DIR= DATA_DIR / "warehouse"
REJECTS_DIR  = DATA_DIR / "rejects"
LOGS_DIR     = DATA_DIR / "logs"
REPORTS_DIR  = PROJECT_ROOT / "reports"

# ── Database files ────────────────────────────────────────────────────────────
STAGING_DB   = STAGING_DIR  / "staging.db"
WAREHOUSE_DB = WAREHOUSE_DIR/ "warehouse.db"
REJECTS_DB   = REJECTS_DIR  / "rejects.db"
RUN_HISTORY  = LOGS_DIR     / "run_history.jsonl"

# ── Source file patterns ──────────────────────────────────────────────────────
CUSTOMERS_FILE  = RAW_DIR / "customers.csv"
PRODUCTS_FILE   = RAW_DIR / "products.csv"
ORDERS_GLOB     = "orders_*.csv"          # matched inside RAW_DIR
WEB_EVENTS_FILE = RAW_DIR / "web_events.json"

# ── Normalisation maps ────────────────────────────────────────────────────────
COUNTRY_MAP = {
    "u.s.a": "United States", "usa": "United States", "us": "United States",
    "united states": "United States",
    "ca": "Canada", "canada": "Canada",
    "uk": "United Kingdom", "united kingdom": "United Kingdom",
    "de": "Germany", "germany": "Germany",
    "in": "India", "india": "India",
}

CATEGORY_MAP = {
    "apparel": "Apparel", "electronics": "Electronics",
    "books": "Books", "toys": "Toys", "home": "Home",
}

STATUS_MAP = {
    "pending": "Pending", "shipped": "Shipped",
    "delivered": "Delivered", "cancelled": "Cancelled",
}

KNOWN_STATUSES   = set(STATUS_MAP.values())
KNOWN_CATEGORIES = set(CATEGORY_MAP.values())

# ── Quality gate thresholds ───────────────────────────────────────────────────
MIN_CUSTOMERS  = 10
MIN_PRODUCTS   = 5
MIN_ORDERS     = 10
