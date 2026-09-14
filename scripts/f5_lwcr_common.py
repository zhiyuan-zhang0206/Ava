"""Shared helpers for the F5 (task #3384) launchd LWCR/EX_CONFIG harnesses.

Two scenario scripts build a throwaway .app, sign it (ad-hoc, or with the
production code-signing certificate), load it under a test launchd label and
sample the xpcproxy/smd/BTM log surface:

    scripts/lwcr_fault_injection.py    legacy LaunchAgent, fault injection
    scripts/f5_lwcr_smappservice.py    SMAppService/BTM-registered agent

The production helper is never touched; every artifact lives under the
scenario's own --workdir (never /tmp).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]
# Allow `python scripts/<scenario>.py` (sys.path[0] = scripts/) to resolve the
# checkout's services package; under pytest pythonpath=["."] this is a no-op.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

HELPER_BUNDLE_ID = "com.ava.permissions-helper"
_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
_POLL_S = 0.25


class F5Error(RuntimeError):
    """One phase invariant was violated; the message carries the phase."""

    def __init__(self, phase: str, detail: str) -> None:
        super().__init__(f"FAIL(phase={phase}): {detail}")
        self.phase = phase
        self.detail = detail


def _fail(phase: str, detail: str) -> NoReturn:
    raise F5Error(phase, detail)


def _cap(cmd: list[str], *, timeout: float = 120.0) -> tuple[int, str]:
    """Run one fixed-argv command; return (rc, combined output). Never raises."""
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {' '.join(cmd)}"
    return proc.returncode, proc.stdout + proc.stderr


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _field(text: str, name: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _job_verdict(label: str) -> dict[str, Any]:
    _, text = _cap(["launchctl", "print", f"{_domain()}/{label}"], timeout=30.0)
    exit_code = _field(text, "last exit code")
    exit_match = re.match(r"(\d+)", exit_code) if exit_code else None
    pid = _field(text, "pid")
    return {
        "state": _field(text, "state"),
        "job_state": _field(text, "job state"),
        "last_exit_code": int(exit_match.group(1)) if exit_match else None,
        "runs": _field(text, "runs"),
        "pid": int(pid) if pid and pid.isdigit() else None,
        "raw": text,
    }


def _cdhash(target: Path) -> str:
    _, out = _cap(["codesign", "-dvvv", str(target)], timeout=60.0)
    match = re.search(r"CDHash=([0-9a-f]+)", out)
    return match.group(1) if match else "<unreadable>"


def _wait_for(what: str, predicate: Callable[[], Any], timeout: float, phase: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(_POLL_S)
    _fail(phase, f"timed out after {timeout:.0f}s waiting for {what}")


class Evidence:
    """Workdir evidence writer; every artifact lands under <workdir>/evidence."""

    def __init__(self, workdir: Path) -> None:
        self.dir = workdir / "evidence"
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text.rstrip("\n") + "\n")

    def note(self, name: str, cmd: list[str], rc: int, out: str) -> None:
        self.save(name, f"$ {' '.join(str(part) for part in cmd)}\nrc = {rc}\n---\n{out}")


def _build_binary(source: Path, out_binary: Path) -> None:
    rc, out = _cap(["swiftc", "-O", str(source), "-o", str(out_binary)], timeout=300.0)
    if rc != 0:
        _fail("build", f"swiftc failed (rc={rc}): {out[-800:]}")


def _cert_signing_context() -> None:
    """lifecycle's own signing preflight: it refuses instead of prompting."""
    from services.permissions_helper import lifecycle as helper_lifecycle

    helper_lifecycle.preflight_signing_smoke()


def _cert_leaf_hash() -> str:
    from services.permissions_helper import lifecycle as helper_lifecycle

    rc, out = _cap(["security", "find-identity", "-p", "codesigning"], timeout=30.0)
    match = helper_lifecycle._IDENTITY_RE.search(out) if rc == 0 else None
    if match is None:
        _fail("build", f"code-signing identity {helper_lifecycle._CERT_CN!r} unresolved (rc={rc})")
    return match.group(1).lower()


def _sign_app(app: Path, sign_mode: str, *, identifier: str = HELPER_BUNDLE_ID) -> None:
    if sign_mode == "none":
        return
    if sign_mode == "cert":
        from services.permissions_helper import lifecycle as helper_lifecycle

        cert_cn = helper_lifecycle._CERT_CN
        dr = f'identifier "{identifier}" and certificate leaf = H"{_cert_leaf_hash()}"'
        cmd = [
            "codesign",
            "--force",
            "--sign",
            cert_cn,
            "--identifier",
            identifier,
            "--requirements",
            f"=designated => {dr}",
            str(app),
        ]
    else:
        cmd = ["codesign", "--force", "--sign", "-", "--identifier", identifier, str(app)]
    rc, out = _cap(cmd, timeout=180.0)
    if rc != 0:
        _fail("build", f"codesign failed (rc={rc}): {out[-800:]}")


def _bootout(label: str) -> tuple[int, str]:
    return _cap(["launchctl", "bootout", f"{_domain()}/{label}"], timeout=30.0)


def _bootstrap(plist: Path) -> tuple[int, str]:
    return _cap(["launchctl", "bootstrap", _domain(), str(plist)], timeout=60.0)


def _log_window(evidence: Evidence, name: str, predicate: str, last: str = "5m") -> None:
    rc, out = _cap(
        ["log", "show", "--style", "compact", "--last", last, "--predicate", predicate],
        timeout=240.0,
    )
    evidence.note(name, ["log", "show", f"--predicate '{predicate}'", f"--last {last}"], rc, out)


def _start_lwcr_stream(evidence: Evidence, name: str) -> tuple[subprocess.Popen[str], Any]:
    handle = (evidence.dir / name).open("w")
    predicate = (
        'eventMessage CONTAINS "LWCR" OR eventMessage CONTAINS "spawn failed" '
        'OR process == "xpcproxy" OR process == "smd" OR process == "backgroundtaskmanagementd" '
        'OR process == "BackgroundTaskManagementAgent" OR eventMessage CONTAINS "Constraint" '
        'OR process == "amfid" OR process == "syspolicyd"'
    )
    proc = subprocess.Popen(  # noqa: S603
        ["log", "stream", "--style", "compact", "--level", "debug", "--predicate", predicate],
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc, handle


def _stop_lwcr_stream(proc: subprocess.Popen[str], handle: Any) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    handle.close()
