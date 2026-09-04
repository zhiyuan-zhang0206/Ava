"""Cold-import boundary guards for the memory indexer daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[2]
_BACKEND_PREFIX = "services.memory_indexer.backends."
_CONCRETE_BACKENDS = {
    f"{_BACKEND_PREFIX}milvus",
    f"{_BACKEND_PREFIX}numpy",
    f"{_BACKEND_PREFIX}pgvector",
}


def _run_fresh_daemon_import(tmp_path: Path, code: str) -> subprocess.CompletedProcess[str]:
    """Import the daemon in a fresh interpreter with an isolated unit home."""
    (tmp_path / ".env").write_text(
        "AVA_MACHINE_SERVE_GATEWAY=true\n"
        "AVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
        "AVA_DB_URL=postgresql://unprovisioned@127.0.0.1:1/unprovisioned\n"
        "AVA_REDIS_URL=redis://127.0.0.1:1/0\n"
        "AVA_CLUSTER_SECRET=test-cluster-secret\n"
        "AVA_OS_JOBS_ENABLED=false\n"
        "AVA_TELEMETRY_OTLP_ENABLED=false\n"
        "AVA_PGBOUNCER_ENABLED=false\n"
        "AVA_TRANSPORT_ENCRYPTION=overlay\n"
    )
    (tmp_path / "machine_name").write_text("test-box")
    env = os.environ.copy()
    env["AVA_HOME"] = str(tmp_path)
    env["AVA_CONFIG_FETCH"] = "skip"
    env.pop("AVA_PROCESS_PROFILE", None)
    env.pop("AVA_AGENT_ID", None)
    return subprocess.run(  # noqa: S603 — fixed interpreter and repository-owned code
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_daemon_import_does_not_load_backend_implementations(tmp_path: Path) -> None:
    """Importing the daemon must leave every concrete backend unselected."""
    result = _run_fresh_daemon_import(
        tmp_path,
        f"""
import json
import sys

import services.memory_indexer.daemon

prefix = {_BACKEND_PREFIX!r}
print(json.dumps(sorted(name for name in sys.modules if name.startswith(prefix))))
""",
    )

    imported = set(json.loads(result.stdout))
    assert imported.isdisjoint(_CONCRETE_BACKENDS)


def test_daemon_cold_import_completes_within_updater_budget(tmp_path: Path) -> None:
    """The daemon import must remain comfortably inside the updater timeout."""
    result = _run_fresh_daemon_import(
        tmp_path,
        """
import time

started = time.monotonic()
import services.memory_indexer.daemon
print(time.monotonic() - started)
""",
    )

    # 20s leaves broad host variance while catching the 38–52s eager-import regression
    # before the updater's 30s timeout is reached.
    assert float(result.stdout) < 20
