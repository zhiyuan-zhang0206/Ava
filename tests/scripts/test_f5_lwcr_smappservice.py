"""Unit tests for the inject-window classifier in scripts/f5_lwcr_smappservice.py.

`_classify_inject` is the repro/no-repro gate of the F5 conclusion (task
#3384): it decides whether a window of `launchctl print` samples proves the
sticky LWCR/EX_CONFIG(78) loop. CI cannot run the scenario itself (it needs
launchd + BTM), so these tests pin the decision on synthetic sample windows
built from the real observed shapes: spawn-failed-78 (the canonical loop),
the older exit-code-less stuck-spawn flavor, and healthy.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "f5_lwcr_smappservice.py"


def _load_script() -> object:
    spec = importlib.util.spec_from_file_location("f5_lwcr_smappservice", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scenario = _load_script()


def _sample(
    state: str,
    *,
    job_state: str | None = None,
    exit_code: int | None = None,
    runs: str | None = None,
    pid: int | None = None,
) -> dict[str, object]:
    return {
        "state": state,
        "job_state": job_state,
        "last_exit_code": exit_code,
        "runs": runs,
        "pid": pid,
        "heartbeats": 2,
    }


def test_insufficient_samples_is_gated() -> None:
    assert scenario._classify_inject([]) == "insufficient-samples"
    assert scenario._classify_inject([_sample("spawn scheduled")] * 4) == "insufficient-samples"


def test_spawn_failed_78_is_the_canonical_repro() -> None:
    samples = [_sample("xpcproxy", job_state="exited", runs="3")]
    samples += [
        _sample("spawn scheduled", job_state="spawn failed", exit_code=78, runs=str(n))
        for n in range(4, 9)
    ]
    assert scenario._classify_inject(samples) == "spawn-failed-78"


def test_running_sample_is_healthy() -> None:
    samples = [_sample("spawn scheduled")] * 3
    samples += [_sample("running", pid=4242), _sample("running", pid=4242)]
    assert scenario._classify_inject(samples) == "healthy"


def test_spawn_failed_other_code_is_still_reproduced() -> None:
    samples = [
        _sample("spawn scheduled", job_state="spawn failed", exit_code=1, runs=str(n))
        for n in range(3, 9)
    ]
    assert scenario._classify_inject(samples) == "spawn-failed-other-code"


def test_stuck_spawn_requires_runs_growth() -> None:
    growing = [_sample("spawn scheduled", runs=str(n)) for n in range(3, 9)]
    assert scenario._classify_inject(growing) == "stuck-spawn"
    flat = [_sample("spawn scheduled", runs="3")] * 6
    assert scenario._classify_inject(flat) == "stuck-spawn-no-retry"


def test_pid_presence_falls_through_to_other() -> None:
    samples = [_sample("spawn scheduled", runs=str(n)) for n in range(3, 8)]
    samples.append(_sample("spawn scheduled", runs="8", pid=999))
    assert scenario._classify_inject(samples) == "other"


def test_runs_int_parses_first_token() -> None:
    assert scenario._runs_int({"runs": "14"}) == 14
    assert scenario._runs_int({"runs": "7 (throttled)"}) == 7
    assert scenario._runs_int({"runs": None}) is None
    assert scenario._runs_int({"runs": "not-a-number"}) is None
    assert scenario._runs_int({}) is None
