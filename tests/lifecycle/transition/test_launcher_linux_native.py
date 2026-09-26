"""Opt-in native transport/custody proof; not an application A-to-B transition.

The image contains a finite fixture entry, a real copied Python interpreter and
hashed application identity. The production launcher and verifier are unpatched.
Only this test's named system units are mutated, then positively retired.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psutil
import pytest
from pydantic import JsonValue

from cli.release_transition import journal, launcher_linux
from cli.release_transition.request import ReleaseRef, Request
from shared.native_process.ownership import OwnedProcess
from shared.os_boot_unit import unit_name
from shared.runtime_release import file_sha256

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("AVA_NATIVE_RELEASE_LAUNCHER") != "1",
    reason="requires explicit disposable Linux system-manager fixture opt-in",
)

_ENTRY = """import argparse, json, os, subprocess, sys, time
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--operation', type=Path, required=True)
operation = parser.parse_args().operation
directory = operation.parent
(directory / 'fixture-started.json').write_text(json.dumps({
    'pid': os.getpid(), 'ppid': os.getppid(), 'argv': sys.argv,
    'cgroup': Path('/proc/self/cgroup').read_text(),
}))
deadline = time.monotonic() + 60
while not (directory / 'fixture-finish').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('native fixture was not released')
    time.sleep(0.02)
if (directory / 'fixture-retain-child').exists():
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(120)'])
    (directory / 'fixture-child.pid').write_text(str(child.pid))
"""


def _native(argv: list[str]) -> str:
    result = subprocess.run(  # noqa: S603 — exact disposable native units and fixture argv
        argv, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode:
        raise RuntimeError(f"native fixture command failed: {argv!r}: {result.stderr}")
    return result.stdout


def _image(home: Path) -> ReleaseRef:
    assert sys.version_info[:2] == (3, 12)
    digest = hashlib.sha256(_ENTRY.encode()).hexdigest()
    root = home / "releases" / digest
    binary = root / "venv/bin/python"
    site = root / "venv/lib/python3.12/site-packages"
    binary.parent.mkdir(parents=True)
    site.mkdir(parents=True)
    shutil.copyfile(Path(sys.executable).resolve(), binary)
    binary.chmod(0o700)
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
                "platform": "Linux-fixture",
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


def _wait_exit(unit: str) -> None:
    deadline = time.monotonic() + 15
    while launcher_linux._properties(unit)["MainPID"] != "0":
        if time.monotonic() >= deadline:
            raise TimeoutError("fixture executor did not exit")
        time.sleep(0.05)


def _finish_executor(
    request: Request,
    plan: dict[str, JsonValue],
    sibling: OwnedProcess,
    executor: OwnedProcess,
    *,
    retain_child: bool,
) -> dict[str, object]:
    if retain_child:
        (request.path.parent / "fixture-retain-child").touch()
    (request.path.parent / "fixture-finish").touch()
    _wait_exit(str(plan["unit"]))
    if retain_child:
        with pytest.raises(RuntimeError, match="child processes"):
            launcher_linux.readback(plan)
        with pytest.raises(RuntimeError, match="child processes"):
            launcher_linux.retire_current(plan)
        outcome: dict[str, object] = {"retained_child_rejected": True}
    else:
        finished = launcher_linux.retire_current(plan)
        assert finished.finished and finished.exit_code == 1 and finished.exit_status == 0
        assert launcher_linux._properties(str(plan["unit"]))["LoadState"] == "not-found"
        assert launcher_linux.retire_current(plan) == finished
        outcome = {"finished": finished.model_dump(mode="json")}
    assert not executor.live() and sibling.live()
    if retain_child:
        _native(["sudo", "-n", "systemctl", "stop", str(plan["unit"])])
    with pytest.raises(RuntimeError, match="already attempted"):
        launcher_linux.launch(plan)
    return outcome | {"sibling_survived": True}


def _interrupt_and_resume(
    request: Request, plan: dict[str, JsonValue], old: launcher_linux.LinuxJob
) -> tuple[dict[str, JsonValue], launcher_linux.LinuxJob]:
    assert old.owner is not None and old.owner.live()
    with journal.exclusive(request.path) as current:
        current.advance("quiescing")
        current.advance("stopping")
    _native(["sudo", "-n", "systemctl", "kill", "--kill-whom=main", "--signal=SIGKILL", old.unit])
    _wait_exit(old.unit)
    closed = launcher_linux.readback(plan)
    assert closed.finished and not old.owner.live()
    resumed = launcher_linux.resume(plan)
    current = journal.read_operation(request.path)
    assert current.attempt == 1 and current.phase == "stopping" and current.direction == "candidate"
    assert current.retired_executors[0]["terminal"] == closed.model_dump(mode="json")
    assert launcher_linux._properties(old.unit)["LoadState"] == "not-found"
    assert current.launch is not None
    assert resumed.owner is not None and resumed.owner != old.owner and resumed.unit != old.unit
    assert resumed.invocation_id != old.invocation_id
    with pytest.raises(ValueError, match="durable intent"):
        launcher_linux.resume(plan)
    with journal.exclusive(request.path) as updated:
        updated.record_native(resumed.identity)
    return current.launch, resumed


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
        platform_tag="Linux-fixture",
        machine="fixture",
        previous=image.model_copy(update={"artifact_digest": "a" * 64}),
        candidate=image,
        executor=image,
        configuration_digest="f" * 64,
    )
    (home / "releases").mkdir(exist_ok=True)
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


def _retire_fixture(request: Request, sibling: str, births: list[OwnedProcess | None]) -> None:
    current = journal.read_operation(request.path)
    plans = [retired["launch"] for retired in current.retired_executors]
    if current.launch is not None:
        plans.append(current.launch)
    for record in plans:
        plan = launcher_linux.LinuxLaunch.model_validate(record)
        properties = launcher_linux._properties(plan.unit)
        if properties["LoadState"] != "not-found":
            launcher_linux._require_definition(plan, properties)
            _native(["sudo", "-n", "systemctl", "stop", plan.unit])
    if launcher_linux._properties(sibling)["LoadState"] != "not-found":
        _native(["sudo", "-n", "systemctl", "stop", sibling])
    for owner in births:
        if owner is not None:
            assert not owner.live(), "fixture native birth survived exact unit retirement"


@pytest.mark.parametrize("mode", ["finish", "children", "interrupt", "unrecorded"])
def test_native_finite_executor_and_sibling_cgroup(tmp_path: Path, mode: str) -> None:
    request = _operation(tmp_path)
    home = Path(request.home)
    image = request.executor
    plan = launcher_linux.plan_launch(request.path, image.verify(home, request.platform_tag))
    with journal.exclusive(request.path) as current:
        current.record_launch(plan)
    sibling = unit_name(home)
    sibling_birth: OwnedProcess | None = None
    executor_births: list[OwnedProcess] = []
    evidence: dict[str, object] = {"scope": "native systemd transport and custody only"}
    try:
        _native(
            [
                "sudo",
                "-n",
                "systemd-run",
                "--system",
                f"--unit={sibling}",
                "--service-type=exec",
                f"--uid={os.getuid()}",
                "/usr/bin/sleep",
                "120",
            ]
        )
        sibling_pid = int(_native(["systemctl", "show", "--value", "-p", "MainPID", sibling]))
        sibling_birth = OwnedProcess.capture(psutil.Process(sibling_pid))
        job = launcher_linux.launch(plan)
        executor_birth = job.owner
        assert executor_birth is not None
        executor_births.append(executor_birth)
        assert launcher_linux._cgroup(sibling_pid) != job.cgroup
        assert job.cgroup == f"/system.slice/{job.unit}"
        if mode != "unrecorded":
            with journal.exclusive(request.path) as current:
                current.record_native(job.identity)
        evidence["running"] = job.model_dump(mode="json")
        with pytest.raises(RuntimeError, match="already attempted"):
            launcher_linux.launch(plan)
        assert launcher_linux.readback(plan).owner == executor_birth
        if mode in {"interrupt", "unrecorded"}:
            plan, job = _interrupt_and_resume(request, plan, job)
            assert job.owner is not None
            executor_birth = job.owner
            executor_births.append(executor_birth)
            evidence["resumed"] = job.model_dump(mode="json")
            evidence["retired"] = journal.read_operation(request.path).model_dump(mode="json")
        evidence.update(
            _finish_executor(
                request, plan, sibling_birth, executor_birth, retain_child=mode == "children"
            )
        )
        evidence["result"] = "passed"
    except BaseException as exc:
        evidence["result"] = "failed"
        evidence["error"] = repr(exc)
        raise
    finally:
        (tmp_path / "native-proof.json").write_text(json.dumps(evidence, indent=2) + "\n")
        # Also owns a submitted replacement whose readback failed before return.
        _retire_fixture(request, sibling, [*executor_births, sibling_birth])
