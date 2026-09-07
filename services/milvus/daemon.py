"""Milvus-lite standalone gRPC server wrapper.

`os.execvp` replaces the current process with `milvus-lite server
--data-dir ... --port ...`. Same entry pattern as other ava CLI daemons
(gateway / agent-host / labeler /
memory_indexer): `python -m services.<name>.daemon`, unifying the session
session startup logic.

env overrides:
- `AVA_MILVUS_DATA_DIR` — milvus data directory, default
  `~/.ava/milvus-data/`
- `AVA_MILVUS_PORT` — gRPC listen port, default 19530

Only binds `127.0.0.1` — vector DB is a strictly local service, no LAN
port opened.
"""

from __future__ import annotations

import os
import sys

from shared.config import settings
from shared.paths import logs_dir

DATA_DIR = settings.services.milvus_data_dir
PORT = settings.services.milvus_port


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Redirect stdout/stderr to milvus.log before execvp so a crash inside
    # milvus-lite is postmortem-able — after execvp the wrapper process is gone
    # and there is no Python logger left to catch anything. Without this,
    # milvus-lite output only reached the session log and was lost on close
    # (the same blind spot that hid the restarter death).
    log_dir = logs_dir()
    log_fd = os.open(str(log_dir / "milvus.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)
    # execvp replaces the current process image with milvus-lite —
    # signal / cleanup are handled by milvus-lite itself; the session
    # session directly owns the milvus process.
    # The uv-installed entry point lives at .venv/bin/milvus-lite, in
    # PATH because the daemon session env activates the venv
    # (`shared.session_env.forward_env_dict` prepends .venv/bin to PATH).
    try:
        os.execvp(  # noqa: S606 — execvp is intentional, matching the PATH lookup for the milvus-lite binary
            "milvus-lite",
            [
                "milvus-lite",
                "server",
                "--data-dir",
                str(DATA_DIR),
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
            ],
        )
    except OSError as exc:
        # fatal startup error reported before the os.exec replacement; the
        # logger sinks are not initialized in this exec-wrapper daemon yet.
        print(  # noqa: T201
            f"milvus-lite exec failed: {exc} — check whether `uv sync` installed pymilvus[milvus_lite]",
            file=sys.stderr,
        )
        sys.exit(127)


if __name__ == "__main__":
    main()
