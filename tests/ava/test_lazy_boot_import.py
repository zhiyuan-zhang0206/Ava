"""`import ava` must not load the redis / psycopg / otel stacks (task #3816).

Every exec child pays `import ava` before any user code runs. After #3587/#3585
cut the LangChain stack, the remaining weight on that import was the redis
client and the live-events -> agent-snapshot -> psycopg chain (both reached via
`ava.self`) plus the audit -> telemetry chain (via `ava.skills`). Those edges
are function-scoped now (task #3816 M1/M2), so a clean `import ava` — and the
trivial shell/files child riding on it — must leave none of these module
families in `sys.modules`.

Probe mechanics mirror `test_lazy_lm_import.py`: clean subprocess, agent-launch
env vars stripped, repo root forced onto `sys.path` under `-I`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Mirror `test_lazy_lm_import._CLEAN_ENV_STRIP` — with AVA_AGENT_ID forwarded,
# `import ava` self-loads plugin namespaces (`_boot.is_launched_child`),
# inflating the import by ~13MB and adding plugin modules (recon #3586 §2.6).
_CLEAN_ENV_STRIP = frozenset(
    {
        "AVA_AGENT_ID",
        "AVA_RUNNER_MODE",
        "AVA_PROCESS_PROFILE",
        "AVA_EXEC_REQUEST_FILE",
        "AVA_EXEC_RESULT_FILE",
        "AVA_EXEC_TIMEOUT_S",
        "AVA_TURN_ID",
        "AVA_SESSION_ID",
        "AVA_LOG_DIR",
        "AVA_AGENT_LABEL",
        "AVA_AGENT_DIR",
    }
)

# Prefix match covers the module and any submodule under each name.
_FORBIDDEN_PREFIXES = (
    "redis",
    "psycopg",
    "opentelemetry",
)

_PROBE = """
import json
import sys

sys.path.insert(0, {root!r})
import ava  # the import under test

bad = sorted(name for name in sys.modules if name.startswith({prefixes!r}))
print(json.dumps(bad))
"""


def test_import_ava_clean_does_not_load_redis_psycopg_or_otel() -> None:
    code = _PROBE.format(root=str(_REPO_ROOT), prefixes=_FORBIDDEN_PREFIXES)
    env = {key: value for key, value in os.environ.items() if key not in _CLEAN_ENV_STRIP}
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])
    assert loaded == [], (
        f"`import ava` must not load the redis/psycopg/otel stacks, found: {loaded}"
    )
