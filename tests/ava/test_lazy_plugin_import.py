"""Fleet-plugin autoload must not load the redis / psycopg / otel stacks (task #3816).

Every exec child autoloads the `ava_builtins.plugins.ava_fleet` surface. Its
module-level edges used to pull `shared.live_announce` (-> live-events +
redis-client) and `psycopg` (via `task_registry` / `_task_update`); those are
function-scoped now (task #3816 M4a), so importing the plugin family in a clean
process must leave none of these families in `sys.modules` — the trivial
shell/files child must not pay them at boot.

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
# `import ava` self-loads plugin namespaces (`_boot.is_launched_child`); the
# probe must model the producer path, not the launcher's environment.
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
import ava_builtins.plugins.ava_fleet._task_update  # the imports under test
import ava_builtins.plugins.ava_fleet.plugin
import ava_builtins.plugins.ava_fleet.task_registry

bad = sorted(name for name in sys.modules if name.startswith({prefixes!r}))
print(json.dumps(bad))
"""


def test_fleet_plugin_autoload_does_not_load_redis_psycopg_or_otel() -> None:
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
        f"fleet-plugin autoload must not load the redis/psycopg/otel stacks, found: {loaded}"
    )
