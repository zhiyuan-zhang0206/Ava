"""Opt-in native regression for review P2-A: DYLD injection into the signed helper.

A disposable, uniquely labelled job in ``gui/<uid>`` starts this checkout's
helper with ``DYLD_INSERT_LIBRARIES`` in its launchd environment, as
``launchctl setenv`` would supply. The inserted library's constructor records
its process, keeps reporting from a thread while it stays mapped and, for the
finite mode, hides the exec-time environment so the re-exec never happens.

Without the hardened runtime the constructor runs inside the custody process
and ``codesign --verify -R <pid>`` still succeeds, so only the running image's
kernel code-signing status can refuse it. Signed with ``--options runtime``,
dyld ignores the variable: the constructor never runs and admission accepts.
Plists live beside the fixture, never in LaunchAgents; every label is booted out
and verified absent, and only captured births are ever signalled.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
import pytest

from services.permissions_helper import client, finite_artifact, hardened_runtime, lifecycle
from shared.native_process.ownership import OwnedProcess
from tests.lifecycle.transition.macos import native_fixture

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "darwin" or os.environ.get("AVA_NATIVE_RELEASE_LAUNCHER") != "1",
        reason="requires explicit disposable macOS launchd fixture opt-in",
    ),
    pytest.mark.native_permissions_helper,
]

_INJECT = r"""#include <crt_externs.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static char mark[1024];
static void note(const char *what) {
    char line[256];
    int n = snprintf(line, sizeof line, "%s pid=%d\n", what, getpid());
    int fd = open(mark, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd >= 0) { write(fd, line, (size_t)n); close(fd); }
}
static void *linger(void *unused) {
    (void)unused;
    for (;;) { usleep(200000); note("still-loaded"); }
    return NULL;
}
__attribute__((constructor)) static void ava_injected(void) {
    const char *path = getenv("AVA_INJECTION_MARK");
    if (path == NULL) return;
    strlcpy(mark, path, sizeof mark);
    note("constructor");
    if (getenv("AVA_INJECTION_HIDE") != NULL) {
        /* Hide the exec-time environment from the finite mode's scrub check
           (environ shares it, so only the finite mode can run without it). */
        char **argv = *_NSGetArgv();
        argv[*_NSGetArgc() + 1] = NULL;
    }
    pthread_t thread;
    pthread_create(&thread, NULL, linger, NULL);
    pthread_detach(thread);
}
"""


@pytest.fixture(scope="module")
def injected(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("inject").resolve()
    (root / "inject.c").write_text(_INJECT)
    library = root / "inject.dylib"
    native_fixture.run(["clang", "-dynamiclib", str(root / "inject.c"), "-o", str(library)])
    return library


@pytest.fixture(scope="module")
def helpers(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return {
        "unhardened": native_fixture.build_helper_app(
            tmp_path_factory.mktemp("unhardened").resolve(), hardened=False
        ),
        "hardened": native_fixture.build_helper_app(tmp_path_factory.mktemp("hardened").resolve()),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed native tool, disposable exact label
        ["/bin/launchctl", *args], capture_output=True, text=True, timeout=30, check=False
    )


def _wait(condition: Callable[[], bool], what: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"native fixture: {what}")
        time.sleep(0.05)


def _helper_pid(mode: str, directory: Path, socket: Path) -> int:
    if mode == "finite":
        receipt = directory / "group.json"
        _wait(receipt.exists, "finite helper group receipt")
        time.sleep(0.05)
        return int(json.loads(receipt.read_text())["helper_pid"])
    _wait(socket.exists, "helper socket")
    _reply, peer = client.ping_peer(sock_path=socket)
    return peer


def _admission(pid: int) -> str:
    try:
        finite_artifact.require_running_identity(pid, native_fixture.BUNDLE_REQUIREMENT)
    except RuntimeError as exc:
        return f"refused: {exc}"
    return "admitted"


def _job(label: str, executable: Path, mode: str, work: Path, short: Path, library: Path) -> bytes:
    arguments = [str(executable)]
    if mode == "finite":
        receipt = str(work / "group.json")
        arguments += ["--finite-executor", "v1", "--cwd", str(work), "--group-receipt", receipt]
        arguments += ["--", "/bin/sleep", "4"]
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": arguments,
            "EnvironmentVariables": {
                "DYLD_INSERT_LIBRARIES": str(library),
                "AVA_INJECTION_MARK": str(work / "injected.txt"),
                "AVA_PERMISSIONS_HELPER_SOCKET": str(short / "helper.sock"),
                "AVA_PERMISSIONS_HELPER_ROOT_SEED": str(short / "run/seed.json"),
                "AVA_PERMISSIONS_HELPER_SKIP_REGISTRATION": "1",
            }
            | ({"AVA_INJECTION_HIDE": "1"} if mode == "finite" else {}),
            "RunAtLoad": True,
            "KeepAlive": False,
            "AbandonProcessGroup": False,
            "StandardOutPath": str(work / "helper.log"),
            "StandardErrorPath": str(work / "helper.log"),
        }
    )


def _observe(pid: int, mark: Path) -> dict[str, Any]:
    codesign = subprocess.run(  # noqa: S603 — fixed tool, disposable fixture pid
        ["codesign", "--verify", f"-R={native_fixture.BUNDLE_REQUIREMENT}", str(pid)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    admission = _admission(pid)
    time.sleep(1.0)  # the injected thread reports while it stays mapped
    return {
        "helper_pid": pid,
        "running_flags": hardened_runtime.running_code_flags(pid),
        "codesign_running_rc": codesign.returncode,
        "admission": admission,
        "injection": mark.read_text().splitlines() if mark.exists() else [],
    }


def _cleanup(label: str, births: list[OwnedProcess]) -> dict[str, Any]:
    target = f"gui/{os.getuid()}/{label}"
    if _launchctl("print", target).returncode == 0:
        _launchctl("bootout", target)
    _wait(lambda: _launchctl("print", target).returncode == 113, "label absent")
    closed: dict[str, str] = {}
    for birth in births:
        state = "closed"
        if birth.live():
            birth.send_signal(signal.SIGKILL)
            _wait(lambda birth=birth: not birth.live(), "captured birth closed", 10)
            state = "killed-exact-birth"
        closed[str(birth.pid)] = state
    listed = label in _launchctl("list").stdout
    assert not listed
    return {"label": "absent", "births": closed, "listed": listed}


def _require_outcome(signing: str, pid: int, facts: dict[str, Any]) -> None:
    lines, flags, admission = facts["injection"], facts["running_flags"], facts["admission"]
    # The blindness the review found: the running-image signature check passes.
    assert facts["codesign_running_rc"] == 0
    if signing == "unhardened":
        assert f"constructor pid={pid}" in lines, lines
        assert f"still-loaded pid={pid}" in lines, "the library stays mapped in custody"
        assert not flags & hardened_runtime.CS_RUNTIME
        assert admission.startswith("refused") and "hardened runtime" in admission
    else:
        assert lines == [], "dyld must ignore DYLD_INSERT_LIBRARIES for the helper"
        assert hardened_runtime.running_hardened(pid)
        assert admission == "admitted"


@pytest.mark.parametrize("mode", ["finite", "serve"])
@pytest.mark.parametrize("signing", ["unhardened", "hardened"])
def test_injected_library_never_runs_in_an_admitted_helper(
    helpers: dict[str, Path],
    injected: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signing: str,
    mode: str,
) -> None:
    monkeypatch.setattr(lifecycle, "_expected_dr", lambda: native_fixture.BUNDLE_REQUIREMENT)
    work = tmp_path.resolve()
    # Darwin sockaddr_un holds 104 bytes; pytest's temporary paths are too long.
    short = Path(tempfile.mkdtemp(prefix="avhr-", dir="/tmp")).resolve()
    label = f"com.ava.hardened-runtime-fixture.{uuid4().hex[:16]}"
    plist = work / f"{label}.plist"
    executable = helpers[signing] / "Contents/MacOS/AvaPermissionsHelper"
    plist.write_bytes(_job(label, executable, mode, work, short, injected))
    births: list[OwnedProcess] = []
    evidence: dict[str, Any] = {"signing": signing, "mode": mode, "label": label}
    try:
        result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
        assert result.returncode == 0, result.stderr
        pid = _helper_pid(mode, work, short / "helper.sock")
        births.append(OwnedProcess.capture(psutil.Process(pid)))
        facts = _observe(pid, work / "injected.txt")
        if mode == "serve":
            # TCC-dependent reads, no prompt (registration is skipped): the grant
            # is keyed on the designated requirement, which hardening keeps.
            reply = client.ping(sock_path=short / "helper.sock")
            facts["tcc"] = {key: reply.get(key) for key in ("preflight_screen", "ax_trusted")}
        evidence |= facts | {"running_flags": hex(facts["running_flags"])}
        _require_outcome(signing, pid, facts)
        evidence["result"] = "passed"
    except BaseException as exc:
        evidence["result"] = f"failed: {exc!r}"
        raise
    finally:
        evidence["cleanup"] = _cleanup(label, births)
        proof = json.dumps(evidence, indent=2) + "\n"
        (work / "native-proof.json").write_text(proof)
        if directory := os.environ.get("AVA_NATIVE_EVIDENCE_DIR"):
            Path(directory, f"{label}.json").write_text(proof)
        shutil.rmtree(short)
