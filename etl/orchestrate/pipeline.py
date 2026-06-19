"""
pipeline.py — Orchestration layer.

Runs: Extract → Transform → Load → Quality Gates
Structured logging written to data/logs/ and printed to stdout.
Appends a JSON run summary to run_history.jsonl.
"""
import argparse
import json
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

from etl.config import LOGS_DIR, RUN_HISTORY
from etl.extract.extractors  import run_extract
from etl.transform.transformers import run_transform
from etl.load.loader          import run_load
from etl.quality.gates        import run_quality_gates, QualityGateError


# ── logging setup ─────────────────────────────────────────────────────────────

def _setup_logging() -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts      = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    logfile = LOGS_DIR / f"run_{ts}.log"

    fmt = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(logfile),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logfile


def _append_history(record: dict) -> None:
    RUN_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(RUN_HISTORY, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── main pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(reset_incremental: bool = False) -> dict:
    logfile = _setup_logging()
    log     = logging.getLogger("pipeline")
    started = datetime.utcnow().isoformat()

    log.info("=" * 60)
    log.info("ETL PIPELINE START")
    log.info(f"  reset_incremental = {reset_incremental}")
    log.info("=" * 60)

    summary = {
        "started_at": started,
        "status": "running",
        "reset_incremental": reset_incremental,
        "logfile": str(logfile),
        "stages": {},
    }

    try:
        # ── Extract ───────────────────────────────────────────────────────────
        log.info("── STAGE: extract ──")
        extract_result = run_extract(reset=reset_incremental)
        summary["stages"]["extract"] = extract_result
        log.info(f"extract complete: {extract_result}")

        # ── Transform ─────────────────────────────────────────────────────────
        log.info("── STAGE: transform ──")
        clean = run_transform()
        transform_result = {k: len(v) for k, v in clean.items()}
        summary["stages"]["transform"] = transform_result
        log.info(f"transform complete: {transform_result}")

        # ── Load ──────────────────────────────────────────────────────────────
        log.info("── STAGE: load ──")
        load_result = run_load(clean)
        summary["stages"]["load"] = load_result
        log.info(f"load complete: {load_result}")

        # ── Quality Gates ─────────────────────────────────────────────────────
        log.info("── STAGE: quality gates ──")
        passed = run_quality_gates()
        summary["stages"]["quality_gates"] = {"passed": passed}
        log.info(f"quality gates passed: {passed}")

        # ── Done ──────────────────────────────────────────────────────────────
        summary["status"]     = "success"
        summary["finished_at"]= datetime.utcnow().isoformat()
        log.info("=" * 60)
        log.info("ETL PIPELINE COMPLETE ✓")
        log.info("=" * 60)

    except QualityGateError as e:
        summary["status"]     = "failed_quality_gate"
        summary["error"]      = str(e)
        summary["finished_at"]= datetime.utcnow().isoformat()
        log.error(f"QUALITY GATE FAILED: {e}")
        _append_history(summary)
        raise

    except Exception as e:
        summary["status"]     = "failed"
        summary["error"]      = str(e)
        summary["traceback"]  = traceback.format_exc()
        summary["finished_at"]= datetime.utcnow().isoformat()
        log.error(f"PIPELINE ERROR: {e}")
        _append_history(summary)
        raise

    _append_history(summary)
    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the ETL pipeline")
    parser.add_argument(
        "--reset-incremental", action="store_true",
        help="Clear the order file log and reprocess all order files from scratch"
    )
    args = parser.parse_args()
    run_pipeline(reset_incremental=args.reset_incremental)
