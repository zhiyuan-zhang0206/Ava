"""`python -m services.hierarchy_worker.job --job-id N` — one build child.

The entry point the worker's parent process spawns (runner.run_child). The
build logic lives in `services.hierarchy_worker.execute.execute_job`; this
module is only argv plumbing, so tests and the runner can call the logic
directly — plus the child's process boot: `shared/log.py` drops loguru's
default handler at import, so without the `init_gateway_process` seam every
record this process produces — including the `llm_usage` rows that are the
worker's authoritative per-call metering ledger — is silently discarded
(task #3868).
"""

from __future__ import annotations

import argparse
import sys

from shared.log import init_gateway_process


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one hierarchy build job.")
    parser.add_argument("--job-id", type=int, required=True, help="hierarchy_jobs row id")
    args = parser.parse_args(argv)
    # Process boot: the per-daemon log file, stderr, and the unified event
    # pipeline. The name is the child's dimension in every sink
    # (`schedule-hierarchy-worker` is the host running prepare()); the boot
    # row doubles as the per-job start record.
    init_gateway_process(name="hierarchy-worker")
    from services.hierarchy_worker.execute import execute_job

    return execute_job(args.job_id)


if __name__ == "__main__":
    sys.exit(main())
