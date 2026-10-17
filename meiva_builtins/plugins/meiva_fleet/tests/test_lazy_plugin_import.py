"""Fleet-plugin autoload must not load the redis / psycopg / otel stacks (task #3816).

Every exec child autoloads the `meiva_builtins.plugins.meiva_fleet` surface. Its
module-level edges used to pull `base.events.live.announce` (-> live-events +
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

_REPO_ROOT = Path(__file__).resolve().parents[4]

# Mirror `test_lazy_lm_import._CLEAN_ENV_STRIP` — with MEIVA_AGENT_ID forwarded,
# `import meiva` self-loads plugin namespaces (`agent_identity.is_launched_child`); the
# probe must model the producer path, not the launcher's environment.
_CLEAN_ENV_STRIP = frozenset(
    {
        "MEIVA_AGENT_ID",
        "MEIVA_PROCESS_PROFILE",
        "MEIVA_EXEC_REQUEST_FILE",
        "MEIVA_EXEC_RESULT_FILE",
        "MEIVA_EXEC_TIMEOUT_S",
        "MEIVA_TURN_ID",
        "MEIVA_SESSION_ID",
        "MEIVA_LOG_DIR",
        "MEIVA_AGENT_LABEL",
        "MEIVA_AGENT_DIR",
    }
)

# Prefix match covers the module and any submodule under each name.
_FORBIDDEN_PREFIXES = (
    "redis",
    "psycopg",
    "opentelemetry",
)

# Literal source with data in argv, so test selection can read the probe's imports.
_PROBE = """
import json
import sys

root, prefixes = sys.argv[1], tuple(json.loads(sys.argv[2]))
sys.path.insert(0, root)
import meiva_builtins.plugins.meiva_fleet._task_update  # the imports under test
import meiva_builtins.plugins.meiva_fleet.plugin
import meiva_builtins.plugins.meiva_fleet.task_registry

bad = sorted(name for name in sys.modules if name.startswith(prefixes))
print(json.dumps(bad))
"""


def test_fleet_plugin_autoload_does_not_load_redis_psycopg_or_otel() -> None:
    env = {key: value for key, value in os.environ.items() if key not in _CLEAN_ENV_STRIP}
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [
            sys.executable,
            "-I",
            "-B",
            "-X",
            "utf8",
            "-c",
            _PROBE,
            str(_REPO_ROOT),
            json.dumps(_FORBIDDEN_PREFIXES),
        ],
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
