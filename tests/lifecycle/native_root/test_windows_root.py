"""Native Windows evidence; skipped elsewhere rather than simulating Win32 success."""
# ruff: noqa: S603 -- fixed Python fixtures inside a disposable home, no external commands

import json
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="native Windows Job/pipe/console proof"
)
REPO = Path(__file__).resolve().parents[3]


def wait_for(predicate, detail: str, timeout: float = 10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(detail)


def ended(process: psutil.Process) -> bool:
    try:
        process.wait(timeout=0)
        return True
    except psutil.TimeoutExpired:
        return False
    except psutil.NoSuchProcess:
        return True


@contextmanager
def root_fixture(
    tmp_path: Path,
    env: dict[str, str],
    code: str,
    *,
    ignore_break: bool = False,
    terminal_broker: bool = False,
):
    from shared.root_control.client import RootClient, native_identity

    run = Path(env["AVA_HOME"]) / "run" / "ava-root"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "units": [
                    {"id": "svc", "exec": [sys.executable, "-u", "-c", code], "restart": "never"}
                ]
            }
        )
    )
    log = (tmp_path / "root.log").open("wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "services.ava_root",
            "--run-dir",
            str(run),
            "--manifests",
            str(manifest),
            *(["--wiring", "tests.lifecycle.native_root.wiring:build"] if terminal_broker else []),
        ],
        cwd=REPO,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    client = RootClient(run / "ava-root.sock", timeout=2)
    captured_root = None

    def ready():
        nonlocal captured_root
        if proc.poll() is not None:
            raise AssertionError((tmp_path / "root.log").read_text())
        try:
            response = client.status()
            captured_root = native_identity(response["result"]["root"])
            return response["ok"]
        except Exception:
            return False

    try:
        wait_for(ready, "native root did not become ready")
        yield proc, client, run, manifest
    finally:
        if proc.poll() is None:
            with suppress(Exception):
                if ignore_break:
                    client.force_down("svc")
                client.shutdown()
                proc.wait(timeout=15)
            if proc.poll() is None:
                if captured_root is not None and captured_root.live():
                    psutil.Process(captured_root.pid).kill()
                else:
                    proc.kill()
                proc.wait(timeout=5)
        log.close()


def sleeping_service(receipt: Path, *, ignore: bool = False) -> str:
    child_ready = receipt.with_name(receipt.name + "-ready")

    def handler(marker: Path) -> str:
        if ignore:
            return f"lambda *args: pathlib.Path({str(marker)!r}).write_text('observed')"
        return "lambda *args: exit(0)"

    child = f"""import signal,time,pathlib
signal.signal(signal.SIGBREAK, {handler(receipt.with_name(receipt.name + "-child-break"))})
pathlib.Path({str(child_ready)!r}).write_text('ready')
while True: time.sleep(0.02)
"""
    return f"""import os,signal,time,pathlib,subprocess,sys
signal.signal(signal.SIGBREAK, {handler(receipt.with_name(receipt.name + "-break"))})
p=subprocess.Popen([sys.executable,'-u','-c',{child!r}])
while not pathlib.Path({str(child_ready)!r}).exists(): time.sleep(0.01)
pathlib.Path({str(receipt)!r}).write_text(str(p.pid))
while True: time.sleep(0.02)
"""


def test_native_protocol_generation_and_graceful_job_closure(tmp_path, native_env):
    receipt = tmp_path / "member"
    with root_fixture(tmp_path, native_env, sleeping_service(receipt)) as (proc, client, run, _):
        wait_for(receipt.exists, "service grandchild did not start")
        member = psutil.Process(int(receipt.read_text()))
        initial = client.status()["result"]
        assert client.up("svc")["ok"]
        repeated = client.status()["result"]
        assert initial["units"][0]["pid"] == repeated["units"][0]["pid"]
        assert initial["units"][0]["create_time"] == repeated["units"][0]["create_time"]
        assert client.down("svc")["ok"]
        wait_for(lambda: ended(member), "graceful down left a Job member")
        assert not list((run / "custody").iterdir())
        assert client.shutdown()["ok"]
        assert proc.wait(timeout=10) == 0


def test_root_death_kills_entire_job_but_retains_custody(tmp_path, native_env):
    receipt = tmp_path / "member"
    with root_fixture(tmp_path, native_env, sleeping_service(receipt)) as (
        proc,
        client,
        run,
        manifest,
    ):
        wait_for(receipt.exists, "service grandchild did not start")
        member = psutil.Process(int(receipt.read_text()))
        psutil.Process(client.status()["result"]["root"]["pid"]).kill()
        proc.wait(timeout=5)
        wait_for(lambda: ended(member), "root death left a Job member")
        assert (run / "custody/svc.json").exists()
        second = subprocess.run(
            [
                sys.executable,
                "-m",
                "services.ava_root",
                "--run-dir",
                str(run),
                "--manifests",
                str(manifest),
            ],
            cwd=REPO,
            env=native_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert second.returncode != 0
        assert "custody requires reconciliation" in second.stderr


def test_force_job_closure_keeps_unrelated_process_alive(tmp_path, native_env):
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], env=native_env
    )
    try:
        receipt = tmp_path / "member"
        with root_fixture(
            tmp_path, native_env, sleeping_service(receipt, ignore=True), ignore_break=True
        ) as (_, client, run, _):
            wait_for(receipt.exists, "service grandchild did not start")
            member = psutil.Process(int(receipt.read_text()))
            assert client.force_down("svc")["ok"]
            wait_for(lambda: ended(member), "force down left a Job member")
            assert unrelated.poll() is None
            assert not list((run / "custody").iterdir())
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_native_pipe_rejects_bad_frames_and_recovers(tmp_path, native_env):
    from shared.root_control.ipc import MAX_MESSAGE_BYTES
    from shared.root_control.windows.transport import roundtrip

    with root_fixture(tmp_path, native_env, "import time; time.sleep(120)", ignore_break=True) as (
        _,
        client,
        run,
        _,
    ):
        raw, peer = roundtrip(run / "ava-root.sock", b'{"verb":"invented"}\n', 2)
        assert json.loads(raw)["code"] == "unknown_verb"
        assert peer == client.status()["result"]["root"]["pid"]
        with pytest.raises((OSError, ValueError)):
            roundtrip(run / "ava-root.sock", b"x" * (MAX_MESSAGE_BYTES + 1) + b"\n", 0.5)
        assert client.status()["ok"]


def test_native_singleton(tmp_path, native_env):
    with root_fixture(tmp_path, native_env, "import time; time.sleep(120)", ignore_break=True) as (
        _,
        _,
        run,
        manifest,
    ):
        second = subprocess.run(
            [
                sys.executable,
                "-m",
                "services.ava_root",
                "--run-dir",
                str(run),
                "--manifests",
                str(manifest),
            ],
            cwd=REPO,
            env=native_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert second.returncode != 0
        assert "another root supervisor" in second.stderr


def test_failed_graceful_shutdown_retains_root_control_and_job(tmp_path, native_env):
    receipt = tmp_path / "member"
    with root_fixture(
        tmp_path,
        native_env,
        sleeping_service(receipt, ignore=True),
        ignore_break=True,
    ) as (root, client, run, _):
        wait_for(receipt.exists, "service grandchild did not start")
        member = psutil.Process(int(receipt.read_text()))
        assert client.shutdown()["ok"]
        wait_for(
            lambda: "retains custody after failed shutdown" in (tmp_path / "root.log").read_text(),
            "failed graceful closure did not retain a controllable root",
            timeout=20,
        )
        assert root.poll() is None
        assert client.status()["ok"]
        assert (tmp_path / "member-break").read_text() == "observed"
        assert (tmp_path / "member-child-break").read_text() == "observed"
        assert "application Job still has members" in (tmp_path / "root.log").read_text()
        assert member.is_running()
        assert (run / "custody/svc.json").exists()
        assert client.force_down("svc")["ok"]
        wait_for(lambda: ended(member), "explicit force did not close the retained Job")
        assert client.shutdown()["ok"]
        assert root.wait(timeout=10) == 0
