"""
schedule_pipeline.py -- Simple scheduler wrapper.

On Linux/macOS add to crontab:
    0 6 * * * cd /path/to/etl_pipeline_project && python schedule_pipeline.py

On Windows add a Task Scheduler action:
    Program:  python
    Arguments: C:\\path\\to\\etl_pipeline_project\\schedule_pipeline.py
    Start in: C:\\path\\to\\etl_pipeline_project

The script runs the full pipeline and exits with code 0 on success,
1 on any failure, so the scheduler can detect failures.
"""
import sys
from etl.orchestrate.pipeline import run_pipeline

if __name__ == "__main__":
    try:
        run_pipeline()
        sys.exit(0)
    except Exception as e:
        print(f"SCHEDULER: pipeline failed -- {e}", file=sys.stderr)
        sys.exit(1)
