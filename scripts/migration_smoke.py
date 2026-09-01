#!/usr/bin/env python
"""Run scripts/test_migrations_apply.sh against a throwaway native Postgres.

The migration smoke applies baseline-pending migrations in order + exercises
trigger paths on a fresh DB (catches migration bodies referencing wrong columns
— see the script's header). It needs a real Postgres server; this wrapper starts
an ephemeral native cluster (no Docker) via the shared `throwaway_postgres`
helper.
"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from shared.pg_tools import throwaway_postgres

_REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    with throwaway_postgres() as url:
        u = urlparse(url)
        if not (u.hostname and u.port and u.username):
            raise RuntimeError(f"malformed cluster url: {url}")
        env = {
            **os.environ,
            "PGHOST": u.hostname,
            "PGPORT": str(u.port),
            "PGUSER": u.username,
            "PGPASSWORD": u.password or "",
            "PGDATABASE": u.path.lstrip("/"),
        }
        proc = subprocess.run(
            ["bash", "scripts/test_migrations_apply.sh"], cwd=_REPO_ROOT, env=env, check=False
        )
        return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
