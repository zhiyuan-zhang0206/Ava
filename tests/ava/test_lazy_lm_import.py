"""`import ava` must not load the LM stack — startup-path laziness (task #3587).

Every exec child pays `import ava` before any user code runs; that import must
stop at the config/effort layer and never pull langchain / langgraph /
langsmith or the provider API (`shared.lm.provider_api`). The attachment
constants live in an import-free leaf (`shared.lm.attach_constants`) precisely
so the SDK surfaces do not drag the LangChain-backed packing machinery in.

The probe runs in a clean subprocess (isolated interpreter, agent-launch env
vars stripped) and reports every forbidden module left in `sys.modules`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Agent-launch markers: with AVA_AGENT_ID forwarded, `import ava` self-loads
# plugin namespaces (`_boot.is_launched_child`), inflating the import by ~13MB
# and adding plugin modules (recon #3586 §2.6). A boot measurement must strip
# them — mirroring measure-boot.sh's clean env.
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
    "langchain",
    "langgraph",
    "langsmith",
    "shared.lm.provider_api",
)

_PROBE = """
import json
import sys

sys.path.insert(0, {root!r})
import ava  # the import under test

bad = sorted(name for name in sys.modules if name.startswith({prefixes!r}))
print(json.dumps(bad))
"""


def test_import_ava_clean_does_not_load_the_lm_stack() -> None:
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
    assert loaded == [], f"`import ava` must not load the LM stack, found: {loaded}"
