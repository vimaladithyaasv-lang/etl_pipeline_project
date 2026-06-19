# ============================================================
#  api.py — FastAPI Backend  v2.1
#  • Stable table names (no timestamp) = no more duplicates
#  • DELETE /tables        — wipe ALL tables
#  • POST   /cleanup       — remove old timestamped duplicates
#  • GET    /tables        — structured list with row counts
#  • GET    /tables/{name}/data  — paginated row fetch
# ============================================================

from __future__ import annotations

import io
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, inspect, text

SQLITE_URL = "sqlite:///excel_dashboard.db"

def get_engine():
    return create_engine(SQLITE_URL, connect_args={"check_same_thread": False})

app = FastAPI(title="Excel Analyst Dashboard API", version="2.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Helpers ────────────────────────────────────────────────

def _safe_table_name(filename: str, sheet: str) -> str:
    """Stable name = filename + sheet (NO timestamp). Re-import replaces, never duplicates."""
    base = re.sub(r"[^\w]", "_", filename.rsplit(".", 1)[0])
    safe_sheet = re.sub(r"[^\w]", "_", sheet)
    return f"{base}__{safe_sheet}"[:64]

def _detect_and_parse_sheet(xl, sheet_name, max_scan=40):
    df_raw = xl.parse(sheet_name, header=None, dtype=str)
    best_row, best_score = 0, -1
    for i in range(min(max_scan, len(df_raw))):
        row = df_raw.iloc[i].dropna().astype(str)
        row = row[~row.str.strip().str.lower().isin(["none","nan",""])]
        text_like = row[~row.str.match(r"^\s*-?\d+(\.\d+)?\s*$")]
        if len(text_like) > best_score:
            best_score, best_row = len(text_like), i
    df = xl.parse(sheet_name, header=best_row)
    df = df.dropna(how="all").dropna(axis=1, how="all")
    mask = df.apply(lambda r: all(str(v).strip().lower() in ("none","nan","") for v in r), axis=1)
    df = df[~mask].reset_index(drop=True)
    df.columns = [str(c).strip() if not str(c).startswith("Unnamed") else f"col_{i}"
                  for i, c in enumerate(df.columns)]
    for col in df.columns:
        original = df[col].copy()
        converted = pd.to_numeric(df[col], errors="coerce")
        non_null = df[col].dropna()
        if len(non_null) > 0 and converted.notna().sum() / len(non_null) >= 0.60:
            df[col] = converted; continue
        try:
            dt = pd.to_datetime(df[col], infer_datetime_format=True, errors="coerce")
            if dt.notna().sum() / max(len(non_null), 1) >= 0.70:
                df[col] = dt; continue
        except Exception:
            pass
        df[col] = original
    return df, best_row

def _clean_dataframe(df):
    orig = df.shape
    dupes = int(df.duplicated().sum())
    miss_before = int(df.isnull().sum().sum())
    df = df.drop_duplicates().dropna(how="all")
    for col in df.columns:
        if df[col].dtype != object:
            df[col] = df[col].ffill().bfill()
        else:
            df[col] = df[col].fillna("Unknown")
    df.columns = (df.columns.str.strip().str.lower()
                  .str.replace(r"\s+","_",regex=True)
                  .str.replace(r"[^\w]","_",regex=True))
    for col in df.columns:
        if df[col].dtype == object:
            conv = pd.to_numeric(df[col], errors="coerce")
            if conv.notna().sum() / max(df[col].notna().sum(),1) >= 0.60:
                df[col] = conv
    miss_after = int(df.isnull().sum().sum())
    return df, {"original_rows":orig[0],"cleaned_rows":len(df),"original_cols":orig[1],
                "cleaned_cols":len(df.columns),"duplicates_removed":dupes,
                "missing_before":miss_before,"missing_after":miss_after,
                "missing_fixed":miss_before-miss_after}

def _save_to_sqlite(df, table_name):
    df.to_sql(table_name, get_engine(), if_exists="replace", index=False)

def _row_count(engine, tname):
    try:
        with engine.connect() as c:
            return c.execute(text(f'SELECT COUNT(*) FROM "{tname}"')).scalar()
    except Exception:
        return 0

# ── Routes ─────────────────────────────────────────────────

@app.get("/", tags=["health"])
def root():
    return {"status":"ok","service":"Excel Analyst Dashboard API","version":"2.1.0"}

@app.get("/health", tags=["health"])
def health():
    return {"status":"ok","timestamp":datetime.now().isoformat()}

@app.post("/import-excel", tags=["import"])
async def import_excel(file: UploadFile = File(...)):
    """Import every sheet → clean → REPLACE table (stable name, no duplicates)."""
    if not file.filename.endswith((".xlsx",".xls")):
        raise HTTPException(422, "Only .xlsx and .xls files are supported.")
    raw = await file.read()
    try:
        xl = pd.ExcelFile(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(422, f"Cannot open Excel file: {e}")

    results, imported = [], 0
    for sheet in xl.sheet_names:
        info: dict[str,Any] = {"sheet": sheet}
        try:
            df, hdr = _detect_and_parse_sheet(xl, sheet)
            if df.empty:
                info.update({"status":"skipped","reason":"empty","rows":0,"cols":0,"table_name":""})
                results.append(info); continue
            df_clean, stats = _clean_dataframe(df)
            tname = _safe_table_name(file.filename, sheet)
            _save_to_sqlite(df_clean, tname)
            info.update({"status":"ok","table_name":tname,"rows":len(df_clean),
                         "cols":len(df_clean.columns),"columns":list(df_clean.columns),
                         "header_row_detected":hdr,"cleaning_stats":stats})
            imported += 1
        except Exception as e:
            info.update({"status":"error","error":str(e),"rows":0,"cols":0,"table_name":""})
        results.append(info)

    return JSONResponse({"status":"success","filename":file.filename,
                         "sheets_total":len(xl.sheet_names),"sheets_imported":imported,
                         "sheets":results,"imported_at":datetime.now().isoformat()})

@app.get("/tables", tags=["data"])
def list_tables():
    """List all tables with row counts and columns."""
    try:
        engine = get_engine()
        insp = inspect(engine)
        names = [t for t in insp.get_table_names() if not t.startswith("_")]
        rows = []
        for tname in sorted(names):
            cols = [c["name"] for c in insp.get_columns(tname)]
            rows.append({"table_name":tname,"rows":_row_count(engine,tname),
                         "cols":len(cols),"columns":cols})
        return {"tables":rows,"count":len(rows)}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/tables/{table_name}/data", tags=["data"])
def get_table_data(table_name: str, limit: int=1000, offset: int=0):
    """Return paginated rows from a table."""
    try:
        engine = get_engine()
        with engine.connect() as c:
            df = pd.read_sql(f'SELECT * FROM "{table_name}" LIMIT {limit} OFFSET {offset}', c)
        return JSONResponse({"table":table_name,"rows_returned":len(df),"data":df.to_dict(orient="records")})
    except Exception as e:
        raise HTTPException(404, f"Table not found: {e}")

@app.get("/tables/{table_name}/stats", tags=["data"])
def table_stats(table_name: str):
    try:
        engine = get_engine()
        with engine.connect() as c:
            df = pd.read_sql(f'SELECT * FROM "{table_name}"', c)
        num = df.select_dtypes(include="number").columns.tolist()
        return {"table":table_name,"rows":len(df),"cols":len(df.columns),
                "columns":list(df.columns),"numeric_stats":df[num].describe().round(3).to_dict() if num else {}}
    except Exception as e:
        raise HTTPException(404, str(e))

@app.delete("/tables/{table_name}", tags=["data"])
def delete_table(table_name: str):
    try:
        engine = get_engine()
        with engine.connect() as c:
            c.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
            c.commit()
        return {"status":"deleted","table":table_name}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.delete("/tables", tags=["data"])
def delete_all_tables():
    """Wipe EVERY table — full database reset."""
    try:
        engine = get_engine()
        insp = inspect(engine)
        tables = insp.get_table_names()
        with engine.connect() as c:
            for t in tables:
                c.execute(text(f'DROP TABLE IF EXISTS "{t}"'))
            c.commit()
        return {"status":"all_deleted","dropped_count":len(tables),"tables":tables}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/cleanup", tags=["data"])
def cleanup_duplicate_tables():
    """
    Remove OLD timestamped duplicates left from the previous api.py version.
    Groups tables by base name (strips __YYYYMMDD_HHMMSS suffix),
    keeps the latest version of each group, drops the rest.
    """
    try:
        engine = get_engine()
        insp = inspect(engine)
        all_tables = insp.get_table_names()
        ts_pat = re.compile(r"__\d{8}_\d{6}$")
        groups: dict[str,list[str]] = defaultdict(list)
        for t in all_tables:
            groups[ts_pat.sub("", t)].append(t)

        dropped, kept = [], []
        with engine.connect() as c:
            for base, variants in groups.items():
                if len(variants) <= 1:
                    kept.extend(variants); continue
                variants.sort()
                keep = variants[-1]
                kept.append(keep)
                for old in variants[:-1]:
                    c.execute(text(f'DROP TABLE IF EXISTS "{old}"'))
                    dropped.append(old)
            c.commit()
        return {"status":"cleanup_complete","dropped_count":len(dropped),
                "kept_count":len(kept),"dropped_tables":dropped,"kept_tables":kept}
    except Exception as e:
        raise HTTPException(500, str(e))