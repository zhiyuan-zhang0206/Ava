"""Fixed child entry: user code cannot start before the owner's exact permit.

Only the independent owner holds this pipe's write end. The payload receives
neither that handle nor the original host's control writer. EOF is refusal.
"""

import argparse
import os
import runpy
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from shared.exec_owner_protocol import MAX_OWNER_MESSAGE, OwnerControl, read_owner_context


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", type=Path, required=True)
    args = parser.parse_args()
    context = read_owner_context(args.context)
    remaining = (context.allocation.deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise RuntimeError("exec deadline expired before owner permit")

    def expire() -> None:
        time.sleep(remaining)
        os._exit(124)

    threading.Thread(target=expire, daemon=True, name="exec-original-deadline").start()
    raw = sys.stdin.buffer.readline(MAX_OWNER_MESSAGE + 1)
    if not raw or len(raw) > MAX_OWNER_MESSAGE:
        raise RuntimeError("exec owner permit pipe closed or exceeded its bound")
    control = OwnerControl.model_validate_json(raw)
    if (control.request, control.domain, control.action) != (
        context.allocation.request,
        context.allocation.domain,
        "permit",
    ):
        raise RuntimeError("exec owner permit differs from the exact allocation")
    if datetime.now(UTC) >= context.allocation.deadline:
        raise RuntimeError("exec deadline expired while waiting for owner permit")
    with Path(os.devnull).open("rb") as empty:
        os.dup2(empty.fileno(), 0)
    # The actual old child entry, not a second execution engine. Its request
    # and result environment is prepared by the original runtime as before.
    runpy.run_module("agent.exec_child", run_name="__main__")


if __name__ == "__main__":
    main()
