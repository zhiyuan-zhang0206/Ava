"""A zero-record exec child must exit without the telemetry / OTel imports (task #3816 M3).

`agent.exec_child._finalize_telemetry()` is the clean-exit delivery path
(sync queued SDK-call events, flush the OTLP backend). A child that never
emitted a record has no `shared.telemetry` queue to drain, so the helper must
return before importing anything — and in particular must not import the OTel
SDK stack that the old eager `telemetry_otlp.warmup()` brought up at boot.

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

_PROBE = """
import json
import sys

sys.path.insert(0, {root!r})
import agent.exec_child as exec_child  # the module under test

exec_child._finalize_telemetry()  # the zero-record exit path

watched = ("shared.telemetry", "shared.telemetry_otlp")
loaded = sorted(
    name
    for name in sys.modules
    if name.startswith("opentelemetry") or name in watched
)
print(json.dumps(loaded))
"""


def test_zero_record_exit_does_not_import_telemetry_or_otel() -> None:
    code = _PROBE.format(root=str(_REPO_ROOT))
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
        f"a zero-record child exit must not import the telemetry/OTel stacks, found: {loaded}"
    )
