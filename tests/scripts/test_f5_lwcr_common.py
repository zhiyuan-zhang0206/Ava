"""Unit tests for the pure helpers in scripts/f5_lwcr_common.py.

The F5 harnesses need a real launchd job (plus BTM for the SMAppService
scenario) to run end-to-end, so CI cannot execute them; these tests pin the
parse helpers the repro/no-repro verdict rests on — the `launchctl print`
field reader and the `last exit code` normalizer, which must survive the
`78: EX_CONFIG` suffix (task #3384, F5).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "f5_lwcr_common.py"

_STUCK_PRINT = """gui/501/com.ava.test.f5-lwcr-smagent = {
	active count = 0
	state = spawn scheduled
	runs = 14
	last exit code = 78: EX_CONFIG
	job state = spawn failed
	properties = partial import | keepalive | runatload | needs LWCR update
}

	resource coalition = {
		state = active
	}
"""

_RUNNING_PRINT = """gui/501/com.ava.test.f5-lwcr-smagent = {
	state = running
	pid = 4242
	runs = 3
}
"""


def _load_script() -> object:
    spec = importlib.util.spec_from_file_location("f5_lwcr_common", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


common = _load_script()


def _cap_stub(text: str):
    def _fake(cmd: list[str], *, timeout: float = 120.0) -> tuple[int, str]:
        return 0, text

    return _fake


def test_field_reads_exact_key_not_suffix() -> None:
    assert common._field(_STUCK_PRINT, "state") == "spawn scheduled"
    assert common._field(_STUCK_PRINT, "job state") == "spawn failed"
    assert common._field(_STUCK_PRINT, "runs") == "14"
    assert common._field(_STUCK_PRINT, "last exit code") == "78: EX_CONFIG"
    assert common._field(_STUCK_PRINT, "pid") is None
    assert common._field(_RUNNING_PRINT, "pid") == "4242"
    assert common._field(_STUCK_PRINT, "missing key") is None


def test_job_verdict_normalizes_exit_code_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "_cap", _cap_stub(_STUCK_PRINT))
    verdict = common._job_verdict("com.ava.test.f5-lwcr-smagent")
    assert verdict["state"] == "spawn scheduled"
    assert verdict["job_state"] == "spawn failed"
    assert verdict["last_exit_code"] == 78
    assert verdict["runs"] == "14"
    assert verdict["pid"] is None


def test_job_verdict_reads_running_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "_cap", _cap_stub(_RUNNING_PRINT))
    verdict = common._job_verdict("com.ava.test.f5-lwcr-smagent")
    assert verdict["state"] == "running"
    assert verdict["pid"] == 4242
    assert verdict["last_exit_code"] is None


def test_exit_code_int_tolerates_suffixes_and_words() -> None:
    assert common._exit_code_int("78: EX_CONFIG") == 78
    assert common._exit_code_int("0") == 0
    assert common._exit_code_int("killed: 9") is None
    assert common._exit_code_int("") is None
    assert common._exit_code_int(None) is None


def test_job_verdict_killed_run_has_no_numeric_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    canned = _RUNNING_PRINT.replace("	pid = 4242\n", "").replace(
        "	runs = 3\n", "	last exit code = killed: 9\n"
    )
    monkeypatch.setattr(common, "_cap", _cap_stub(canned))
    assert common._job_verdict("x")["last_exit_code"] is None
