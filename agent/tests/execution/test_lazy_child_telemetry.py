"""A zero-record exec child must exit without the telemetry / OTel imports (task #3816 M3).

`agent.execution.child.finalize_telemetry()` is the clean-exit delivery path
(sync queued SDK-call events, flush the OTLP backend). A child that never
emitted a record has no `base.telemetry` queue to drain, so the helper must
return before importing anything — and in particular must not import the OTel
SDK stack that the old eager `telemetry_otlp.warmup()` brought up at boot.

Probe mechanics mirror `test_lazy_lm_import.py`: clean subprocess, agent-launch
env vars stripped; the probes live in `child_lazy_telemetry.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parents[3]

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

_PROBES = Path(__file__).with_name("child_lazy_telemetry.py")


def _run_probe(probe: str) -> object:
    """Run one probe of `child_lazy_telemetry.py` in a clean subprocess; return its JSON report."""
    env = {key: value for key, value in os.environ.items() if key not in _CLEAN_ENV_STRIP}
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-I", "-B", "-X", "utf8", str(_PROBES), probe],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_zero_record_exit_does_not_import_telemetry_or_otel() -> None:
    loaded = _run_probe("zero_record_exit")
    assert loaded == [], (
        f"a zero-record child exit must not import the telemetry/OTel stacks, found: {loaded}"
    )


def test_identity_child_record_holds_without_exporter_imports() -> None:
    """An identity child's first record must be held by the deferral.

    With the sink attached, the boot timing line becomes the child's first
    event-pipeline record — the deferral keeps the export stack (OTel SDK
    logs/exporter/proto + requests/urllib3/protobuf) out of the child's whole
    life so the exporter memory is never paid at boot (task #3816 M4b). The
    record is still delivered: it sits in the backend's bounded hold (`held`).
    """
    result = cast("dict[str, Any]", _run_probe("identity_child_record"))
    assert result["deferred"] is True, "the identity child must arm the deferral"
    assert result["unfinished"] is True, "the boot record must have reached the deferred hold"
    assert result["forbidden"] == [], (
        f"a deferred child must not import the OTLP exporter stack, found: {result['forbidden']}"
    )
