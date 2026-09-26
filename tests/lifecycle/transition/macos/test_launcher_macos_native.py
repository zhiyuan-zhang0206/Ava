"""Opt-in native macOS finite-executor custody proof; not an application A-to-B transition.

The fixture compiles this checkout's permissions helper, signs it ad hoc with an
explicit designated requirement (or runs the production stable-identity build
when AVA_NATIVE_SIGNED_HELPER=1), and builds an image whose finite entry records
its native facts. The production adapter, parser and signature verification run
unpatched against uniquely labelled launchd jobs in the GUI domain; only the
live home-helper lookup is replaced by the fixture binary. Every attempt is
retired by exact label and verified absent; retained descendants are signalled
only through their captured native birth.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
import pytest
from pydantic import JsonValue

from cli.release_transition import journal
from cli.release_transition import launcher_macos as macos
from cli.release_transition.request import ReleaseRef, Request
from services.permissions_helper import finite_artifact, lifecycle
from shared.native_process.ownership import OwnedProcess
from shared.runtime_release import file_sha256

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "darwin" or os.environ.get("AVA_NATIVE_RELEASE_LAUNCHER") != "1",
        reason="requires explicit disposable macOS launchd fixture opt-in",
    ),
    # Owns its disposable helper artifact and exact-label job cleanup.
    pytest.mark.native_permissions_helper,
]

_ENTRY = """import argparse, json, os, subprocess, sys, time
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--operation', type=Path, required=True)
directory = parser.parse_args().operation.parent
tag = f'fixture-{os.getpid()}'
def note(name, value):
    fd = os.open(directory / f'{tag}-{name}.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
note('started', {'pid': os.getpid(), 'ppid': os.getppid(), 'pgid': os.getpgrp(),
                 'sid': os.getsid(0), 'argv': sys.argv, 'cwd': os.getcwd()})
sleeper = [sys.executable, '-I', '-B', '-c', 'import time; time.sleep(120)']
if (directory / 'fixture-child').exists():
    note('child', {'pid': subprocess.Popen(sleeper).pid})
escaped = False
deadline = time.monotonic() + 90
while not (directory / 'fixture-finish').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('native fixture was not released')
    if not escaped and (directory / 'fixture-escape').exists():
        note('escaped', {'pid': subprocess.Popen(sleeper, start_new_session=True).pid})
        escaped = True
    time.sleep(0.02)
"""
_PLATFORM = "macOS-fixture"
_BUNDLE_REQUIREMENT = f'identifier "{lifecycle.HELPER_BUNDLE_ID}"'


def _run(argv: list[str], *, timeout: float = 60) -> str:
    result = subprocess.run(  # noqa: S603 — fixed native tools and disposable fixture paths
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise RuntimeError(f"native fixture command failed: {argv!r}: {result.stderr}")
    return result.stdout


@pytest.fixture(scope="module")
def helper_app(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("helper").resolve()
    if os.environ.get("AVA_NATIVE_SIGNED_HELPER") == "1":
        app, _rebuilt = lifecycle.build_and_sign(destination=root)
        return app
    app = root / "AvaPermissionsHelper.app"
    executable = app / "Contents/MacOS/AvaPermissionsHelper"
    executable.parent.mkdir(parents=True)
    shutil.copyfile(lifecycle._INFO_PLIST, app / "Contents/Info.plist")
    _run(["swiftc", "-O", str(lifecycle._SOURCE), "-o", str(executable)], timeout=300)
    _run(
        [
            "codesign",
            "--force",
            "--sign",
            "-",
            "--identifier",
            lifecycle.HELPER_BUNDLE_ID,
            "--requirements",
            f"=designated => {_BUNDLE_REQUIREMENT}",
            str(app),
        ]
    )
    return app


def _image(home: Path) -> ReleaseRef:
    assert sys.version_info[:2] == (3, 12)
    digest = hashlib.sha256(_ENTRY.encode()).hexdigest()
    root = home / "releases" / digest
    binary = root / "venv/bin/python"
    site = root / "venv/lib/python3.12/site-packages"
    binary.parent.mkdir(parents=True)
    site.mkdir(parents=True)
    interpreter = Path(sys.executable).resolve()
    shutil.copyfile(interpreter, binary)
    binary.chmod(0o700)
    # The interpreter loads libpython through @executable_path/../lib.
    shutil.copyfile(
        interpreter.parent.parent / "lib/libpython3.12.dylib", root / "venv/lib/libpython3.12.dylib"
    )
    (root / "venv/pyvenv.cfg").write_text(
        f"home = {Path(sys.base_prefix) / 'bin'}\ninclude-system-site-packages = false\n"
    )
    package = site / "cli/release_transition"
    package.mkdir(parents=True)
    (site / "cli/__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "execute.py").write_text(_ENTRY)
    identity = site / "shared/release-build.json"
    identity.parent.mkdir()
    identity.write_text(
        json.dumps(
            {
                "version": 1,
                "source_commit": "d" * 40,
                "source_tree": "e" * 40,
                "source_archive_digest": "f" * 64,
                "schema_digest": "c" * 64,
                "applied_names": ["fixture.sql"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    files = {str(p.relative_to(root)): file_sha256(p) for p in root.rglob("*") if p.is_file()}
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "artifact_digest": digest,
                "platform": _PLATFORM,
                "schema_digest": "c" * 64,
                "interpreter": str(binary.relative_to(root)),
                "cwd": str(site.relative_to(root)),
                "files": files,
            },
            sort_keys=True,
        )
    )
    return ReleaseRef(
        artifact_digest=digest,
        manifest_digest=file_sha256(manifest),
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )


def _operation(tmp_path: Path) -> Request:
    home = tmp_path.resolve() / "home"
    home.mkdir(mode=0o700)
    registry = tmp_path.resolve() / "clusters.json"
    registry.write_text("{}")
    image = _image(home)
    request = Request(
        id=uuid4(),
        home=str(home),
        registry=str(registry),
        created_at=datetime.now(UTC),
        platform_tag=_PLATFORM,
        machine="fixture",
        previous=image.model_copy(update={"artifact_digest": "a" * 64}),
        candidate=image,
        executor=image,
        configuration_digest="f" * 64,
    )
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": request.previous.artifact_digest,
                "manifest_digest": request.previous.manifest_digest,
            }
        )
    )
    journal.create(request)
    return request


def _bind_helper(monkeypatch: pytest.MonkeyPatch, app: Path) -> None:
    """Replace only the live home-helper lookup; signature checks stay real."""
    executable = app / "Contents/MacOS/AvaPermissionsHelper"
    monkeypatch.setattr(finite_artifact, "home_helper_executable", lambda: executable)
    if os.environ.get("AVA_NATIVE_SIGNED_HELPER") != "1":
        monkeypatch.setattr(lifecycle, "_expected_dr", lambda: _BUNDLE_REQUIREMENT)


def _note(directory: Path, pid: int, name: str, timeout: float = 15) -> dict[str, Any]:
    path = directory / f"fixture-{pid}-{name}.json"
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"fixture note {path.name} missing")
        time.sleep(0.02)
    time.sleep(0.05)
    return json.loads(path.read_text())


def _capture(pid: int) -> OwnedProcess:
    return OwnedProcess.capture(psutil.Process(pid))


def _terminal(plan: dict[str, JsonValue], timeout: float = 20) -> macos.DarwinJob:
    deadline = time.monotonic() + timeout
    while True:
        try:
            job = macos.readback(plan)
        except RuntimeError:
            job = None
        if job is not None and job.finished:
            return job
        if time.monotonic() >= deadline:
            raise TimeoutError("fixture executor job did not become terminal")
        time.sleep(0.1)


def _require_exec_environment(pid: int, expected: dict[str, str]) -> list[str]:
    """Compare the kernel's exec-time strings (KERN_PROCARGS2) after argv.

    xnu stores envp followed directly by the lowercase apple[] vector (pfz,
    stack_guard, executable_cdhash, ...), so psutil's environ() is not an exact
    oracle. envp must be exactly the helper's sorted pairs; anything after it
    must be an apple[] entry, never a launchd session variable.
    """
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    argmax = ctypes.c_int()
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), "sysctl KERN_ARGMAX")
    buffer = ctypes.create_string_buffer(argmax.value)
    size = ctypes.c_size_t(argmax.value)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), "sysctl KERN_PROCARGS2")
    raw = buffer.raw[: size.value]
    argc = int.from_bytes(raw[:4], sys.byteorder)
    _executable, _, rest = raw[4:].partition(b"\0")
    strings = [item.decode() for item in rest.lstrip(b"\0").split(b"\0")[argc:]]
    pairs = [f"{key}={expected[key]}" for key in sorted(expected)]
    assert strings[: len(pairs)] == pairs
    trailing: list[str] = []
    for item in strings[len(pairs) :]:
        if not item:
            break
        assert re.fullmatch(r"[a-z][a-z0-9_]*=.*", item), f"non-apple exec string: {item!r}"
        trailing.append(item.split("=", 1)[0])
    return trailing


def _running_facts(request: Request, job: macos.DarwinJob) -> dict[str, Any]:
    """Native ancestry, group and exact exec-time environment of one attempt."""
    launch = macos.DarwinLaunch.model_validate(journal.read_operation(request.path).launch)
    assert job.helper is not None and job.executor is not None
    helper = psutil.Process(job.helper.pid)
    executor = psutil.Process(job.executor.pid)
    assert helper.ppid() == 1 and os.getpgid(helper.pid) == helper.pid
    assert helper.cmdline() == launch.program_arguments()
    assert executor.ppid() == helper.pid and os.getpgid(executor.pid) == helper.pid
    # The kernel's exec-time environment: exactly the explicit pairs, none of
    # launchd's session supplements (SSH_AUTH_SOCK, XPC_SERVICE_NAME, ...).
    apple = _require_exec_environment(executor.pid, launch.environment)
    started = _note(request.path.parent, executor.pid, "started")
    assert (started["ppid"], started["pgid"]) == (helper.pid, helper.pid)
    return {
        "launch": launch.label,
        "job": job.model_dump(mode="json"),
        "started": started,
        "exec_environment": sorted(launch.environment),
        "apple_keys_after_envp": apple,
    }


def _launch(mode: str, plan: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch) -> Any:
    if mode == "lost-bootstrap":
        real = macos._command

        def lost(argv: list[str]) -> subprocess.CompletedProcess[str]:
            result = real(argv)
            if argv[1] == "bootstrap":
                raise RuntimeError(f"fixture lost the bootstrap response (rc {result.returncode})")
            return result

        monkeypatch.setattr(macos, "_command", lost)
        with pytest.raises(RuntimeError, match="lost the bootstrap response"):
            macos.launch(plan)
        monkeypatch.setattr(macos, "_command", real)
        with pytest.raises(RuntimeError, match="already attempted"):
            macos.launch(plan)
        return macos._settle(plan)
    if mode == "concurrent":
        outcomes: list[object] = []

        def submit() -> None:
            try:
                outcomes.append(macos.launch(plan))
            except RuntimeError as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        jobs = [item for item in outcomes if isinstance(item, macos.DarwinJob)]
        refused = [item for item in outcomes if isinstance(item, RuntimeError)]
        assert len(jobs) == 1 and len(refused) == 1, outcomes
        assert "already attempted" in str(refused[0])
        return jobs[0]
    return macos.launch(plan)


def _finish(request: Request, plan: dict[str, JsonValue]) -> dict[str, Any]:
    (request.path.parent / "fixture-finish").touch()
    terminal = _terminal(plan)
    assert terminal.exit_code == 0 and terminal.signal is None
    retired = macos.retire_current(plan)
    assert retired == terminal
    assert macos._query(macos.DarwinLaunch.model_validate(plan)) is None
    assert macos.retire_current(plan) == terminal
    with pytest.raises(RuntimeError, match="already attempted"):
        macos.launch(plan)
    return {"terminal": terminal.model_dump(mode="json")}


def _executor_kill(
    request: Request, plan: dict[str, JsonValue], job: macos.DarwinJob, births: list[OwnedProcess]
) -> dict[str, Any]:
    assert job.executor is not None and job.helper is not None
    child = _capture(_note(request.path.parent, job.executor.pid, "child")["pid"])
    births.append(child)
    assert os.getpgid(child.pid) == job.helper.pid
    with journal.exclusive(request.path) as current:
        current.advance("quiescing")
        current.advance("stopping")
    assert job.executor.owned().send_signal(signal.SIGKILL)
    terminal = _terminal(plan)
    assert terminal.exit_code == 81 and macos.FINITE_EXIT[81] == "executor-signaled"
    assert not child.live(), "same-group descendant survived launchd job cleanup"
    (request.path.parent / "fixture-child").unlink()
    resumed = macos.resume(plan)
    current = journal.read_operation(request.path)
    assert (current.attempt, current.phase, current.direction) == (1, "stopping", "candidate")
    assert current.launch is not None and resumed.executor is not None
    replacement = current.launch
    assert macos._query(macos.DarwinLaunch.model_validate(plan)) is None
    with journal.exclusive(request.path) as updated:
        updated.record_native(resumed.identity)
    births.extend(birth.owned() for birth in (resumed.helper, resumed.executor) if birth)
    facts = _running_facts(request, resumed)
    return {
        "killed_terminal": terminal.model_dump(mode="json"),
        "resumed": facts,
        "finished": _finish(request, replacement),
    }


def _helper_kill(
    request: Request, plan: dict[str, JsonValue], job: macos.DarwinJob, births: list[OwnedProcess]
) -> dict[str, Any]:
    assert job.executor is not None and job.helper is not None
    child = _capture(_note(request.path.parent, job.executor.pid, "child")["pid"])
    births.append(child)
    assert job.helper.owned().send_signal(signal.SIGKILL)
    terminal = _terminal(plan)
    assert terminal.signal == 9 and terminal.exit_code is None
    for birth in (job.executor.owned(), child):
        assert not birth.live(), "same-group process survived helper death"
    assert macos.retire_current(plan) == terminal
    return {"terminal": terminal.model_dump(mode="json")}


def _escaped(
    request: Request, plan: dict[str, JsonValue], job: macos.DarwinJob, births: list[OwnedProcess]
) -> dict[str, Any]:
    assert job.executor is not None and job.helper is not None
    (request.path.parent / "fixture-escape").touch()
    escaped = _capture(_note(request.path.parent, job.executor.pid, "escaped")["pid"])
    births.append(escaped)
    assert os.getpgid(escaped.pid) == escaped.pid != job.helper.pid
    with pytest.raises(RuntimeError, match="left the launchd job process group"):
        macos.readback(plan)
    with pytest.raises(RuntimeError, match="left the launchd job process group"):
        macos.retire_current(plan)
    (request.path.parent / "fixture-finish").touch()
    terminal = _terminal(plan)
    # The documented limit: an empty job group and closed recorded births say
    # nothing about a group created elsewhere. The escaped process survives.
    assert terminal.closed is not None and escaped.live()
    assert macos.retire_current(plan) == terminal
    return {"terminal": terminal.model_dump(mode="json"), "escaped_survived_terminal": True}


def _retire_fixture(request: Request, births: list[OwnedProcess]) -> dict[str, Any]:
    current = journal.read_operation(request.path)
    records = [retired["launch"] for retired in current.retired_executors]
    if current.launch is not None:
        records.append(current.launch)
    cleanup: dict[str, Any] = {"labels": {}, "births": {}}
    for record in records:
        launch = macos.DarwinLaunch.model_validate(record)
        if macos._query(launch) is not None:
            _run(["/bin/launchctl", "bootout", launch.target])
        deadline = time.monotonic() + 15
        while macos._query(launch) is not None:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"fixture job {launch.label} was not retired")
            time.sleep(0.05)
        cleanup["labels"][launch.label] = "absent"
    for birth in reversed(births):
        state = "closed"
        if birth.live():
            birth.send_signal(signal.SIGKILL)
            deadline = time.monotonic() + 10
            while birth.live() and time.monotonic() < deadline:
                time.sleep(0.02)
            state = "killed-exact-birth"
        assert not birth.live(), f"fixture birth {birth.pid} survived cleanup"
        cleanup["births"][str(birth.pid)] = state
    listed = _run(["/bin/launchctl", "list"])
    prefix = (
        f"com.ava.release-executor.{hashlib.sha256(str(request.path).encode()).hexdigest()[:32]}"
    )
    assert prefix not in listed
    cleanup["census"] = "no fixture label in launchctl list; every captured birth closed"
    return cleanup


@pytest.mark.parametrize(
    "mode",
    ["finish", "lost-bootstrap", "concurrent", "executor-kill", "helper-kill", "escaped"],
)
def test_native_finite_helper_executor_custody(
    tmp_path: Path, helper_app: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    request = _operation(tmp_path)
    _bind_helper(monkeypatch, helper_app)
    home = Path(request.home)
    plan = macos.plan_launch(request.path, request.executor.verify(home, request.platform_tag))
    with journal.exclusive(request.path) as current:
        current.record_launch(plan)
    if mode in {"executor-kill", "helper-kill"}:
        (request.path.parent / "fixture-child").touch()
    births: list[OwnedProcess] = []
    evidence: dict[str, Any] = {"scope": "native launchd transport and custody only", "mode": mode}
    try:
        job = _launch(mode, plan, monkeypatch)
        assert isinstance(job, macos.DarwinJob) and job.executor is not None
        births.extend(birth.owned() for birth in (job.helper, job.executor) if birth)
        with journal.exclusive(request.path) as current:
            current.record_native(job.identity)
        evidence["helper"] = macos.DarwinLaunch.model_validate(plan).helper.model_dump()
        evidence["running"] = _running_facts(request, job)
        match mode:
            case "executor-kill":
                evidence.update(_executor_kill(request, plan, job, births))
            case "helper-kill":
                evidence.update(_helper_kill(request, plan, job, births))
            case "escaped":
                evidence.update(_escaped(request, plan, job, births))
            case _:
                evidence.update(_finish(request, plan))
        evidence["journal"] = journal.read_operation(request.path).model_dump(mode="json")
        evidence["result"] = "passed"
    except BaseException as exc:
        evidence["result"] = "failed"
        evidence["error"] = repr(exc)
        raise
    finally:
        (request.path.parent / "fixture-finish").touch()
        evidence["cleanup"] = _retire_fixture(request, births)
        (tmp_path / "native-proof.json").write_text(json.dumps(evidence, indent=2) + "\n")
