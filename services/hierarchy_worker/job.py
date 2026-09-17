"""`python -m services.hierarchy_worker.job --job-id N` — one build child.

The entry point the worker's parent process spawns (runner.run_child). The
build logic lives in `services.hierarchy_worker.execute.execute_job`; this
module is only argv plumbing, so tests and the runner can call the logic
directly.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one hierarchy build job.")
    parser.add_argument("--job-id", type=int, required=True, help="hierarchy_jobs row id")
    args = parser.parse_args(argv)
    from services.hierarchy_worker.execute import execute_job

    return execute_job(args.job_id)


if __name__ == "__main__":
    sys.exit(main())
