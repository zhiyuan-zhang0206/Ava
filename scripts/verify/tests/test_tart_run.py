"""The Tart run recipe keeps to its boundary, offline, against a fake `tart` binary.

The rules (future/infra/engineering/verification-boundaries.md): at most two VMs run at once; the one
share is a read-only export of the commit; nothing of the host's cluster, credentials,
keychain or VM store enters; the golden image and earlier VMs are never deleted; the VM a
run made is deleted whatever happens. A real run needs Tart, a golden image and a few
minutes; these tests fail the moment a change to the recipe would break one of those rules.
"""

from __future__ import annotations

import base64
import io
import json
import shlex
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest

from scripts.verify import boundary, tart_run, tart_vm
from scripts.verify.tart_vm import Tart

_FAKE = Path(__file__).resolve().parent / "fake_tart.py"
GOLDEN = "ava-golden"
EARLIER = ("ava-control", "ava-r1", "ava-r2")
OBSERVER_PASSED = json.dumps({"result": "passed", "checks": {}})


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed git argv in a temporary repository
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _logs_archive(files: dict[str, str]) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return base64.b64encode(buffer.getvalue()).decode()


class Fake:
    """A fake Tart installation: a binary, its state and the log of what was asked of it."""

    def __init__(self, directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.directory = directory
        directory.mkdir()
        self.binary = directory / "tart"
        self.binary.write_text(f"#!{sys.executable}\n{_FAKE.read_text()}")
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("FAKE_TART_HOME", str(directory))
        self.state: dict[str, Any] = {
            "vms": {name: {"running": False} for name in (GOLDEN, *EARLIER)},
            "fail_on": [],
            "observer": OBSERVER_PASSED,
            "logs_b64": _logs_archive({"logs/gateway.log": "gateway up\n"}),
        }
        self.save()

    def save(self) -> None:
        (self.directory / "state.json").write_text(json.dumps(self.state))

    def vms(self) -> dict[str, Any]:
        return json.loads((self.directory / "state.json").read_text())["vms"]

    def calls(self) -> list[dict[str, Any]]:
        log = self.directory / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def commands(self, name: str) -> list[list[str]]:
        return [call["argv"] for call in self.calls() if call["argv"][0] == name]

    def tart(self) -> Tart:
        return Tart(str(self.binary))


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    real_sleep = time.sleep

    def quick_sleep(_seconds: float) -> None:
        real_sleep(0.05)

    monkeypatch.setattr(tart_vm.time, "sleep", quick_sleep)
    return Fake(tmp_path / "fake", monkeypatch)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    _git(tmp_path, "init", "--initial-branch=main", str(path))
    (path / "file.txt").write_text("one\n")
    _git(path, "add", "file.txt")
    _git(path, "commit", "-m", "seed")
    (path / "file.txt").write_text("two\n")
    _git(path, "commit", "-am", "second")
    return path


def _run(fake: Fake, repo: Path, tmp_path: Path) -> tuple[int, dict[str, Any], Path]:
    status = tart_run.run("HEAD", tmp_path / "evidence", repo=repo, golden=GOLDEN, tart=fake.tart())
    (root,) = (tmp_path / "evidence").iterdir()
    return status, json.loads((root / "result.json").read_text()), root


# ------------------------------------------------------------------ the run and its cleanup


def test_a_passing_run_copies_the_evidence_out_and_deletes_only_its_vm(
    fake: Fake, repo: Path, tmp_path: Path
) -> None:
    status, result, root = _run(fake, repo, tmp_path)

    assert status == 0 and result["result"] == "passed" and result["removed"] is True
    assert result["commit"] == _git(repo, "rev-parse", "HEAD")
    assert json.loads((root / "observer.json").read_text())["result"] == "passed"
    assert (root / "cluster-logs/logs/gateway.log").read_text() == "gateway up\n"
    assert set(fake.vms()) == {GOLDEN, *EARLIER}
    (deleted,) = fake.commands("delete")
    assert deleted[1] == result["vm"] and deleted[1].startswith(tart_vm.VM_PREFIX)
    assert [step["name"] for step in result["steps"]] == [
        "export", "clone", "boot", "keychain", "prepare-source", "toolchain", "python",
        "profile-upload", "observer-upload", "init", "retire-golden-helper", "start", "observe", "snapshot",
    ]  # fmt: skip

    retirement = next(call for call in fake.commands("exec") if "ava stop" in call[-2])
    assert retirement[1] == result["vm"]


@pytest.mark.parametrize("failing", ["ava stop", "ava start", "uv sync", "git fetch", "ava init"])
def test_a_failed_step_still_collects_and_deletes_the_vm(
    failing: str, fake: Fake, repo: Path, tmp_path: Path
) -> None:
    fake.state["fail_on"] = [failing]
    fake.save()

    status, result, root = _run(fake, repo, tmp_path)

    assert status == 1 and result["result"] == "failed"
    assert "failed" in result["error"]
    assert result["removed"] is True
    assert set(fake.vms()) == {GOLDEN, *EARLIER}
    assert result["collected"] == {"observer.json": True, "cluster-logs": True}
    assert (root / "cluster-logs/logs/gateway.log").is_file()


def test_a_vm_that_will_not_delete_fails_the_run(fake: Fake, repo: Path, tmp_path: Path) -> None:
    class Stubborn(Tart):
        def delete_run_vm(self, name: str) -> bool:
            raise subprocess.CalledProcessError(1, ["tart", "delete", name])

    status = tart_run.run(
        "HEAD", tmp_path / "evidence", repo=repo, golden=GOLDEN, tart=Stubborn(str(fake.binary))
    )

    (root,) = (tmp_path / "evidence").iterdir()
    result = json.loads((root / "result.json").read_text())
    assert status == 1 and result["removed"] is False and "cleanup_error" in result


def test_a_passing_observer_is_not_enough_when_the_vm_stays(
    fake: Fake, repo: Path, tmp_path: Path
) -> None:
    class Leaves(Tart):
        def delete_run_vm(self, name: str) -> bool:
            return False

    status = tart_run.run(
        "HEAD", tmp_path / "evidence", repo=repo, golden=GOLDEN, tart=Leaves(str(fake.binary))
    )
    assert status == 1


def test_an_observer_that_failed_fails_the_run(fake: Fake, repo: Path, tmp_path: Path) -> None:
    fake.state["observer"] = json.dumps({"result": "failed", "checks": {}})
    fake.save()

    status, result, _root = _run(fake, repo, tmp_path)

    assert status == 1 and result["result"] == "failed" and result["removed"] is True


# --------------------------------------------------------------------- the two-VM limit


def test_a_third_vm_is_never_booted(fake: Fake, repo: Path, tmp_path: Path) -> None:
    fake.state["vms"]["ava-r1"]["running"] = True
    fake.state["vms"]["ava-r2"]["running"] = True
    fake.save()

    status, result, _root = _run(fake, repo, tmp_path)

    assert status == 1 and "at most 2" in result["error"]
    assert not fake.commands("clone") and not fake.commands("run")
    assert not fake.commands("delete")
    assert set(fake.vms()) == {GOLDEN, *EARLIER}


def test_one_running_vm_leaves_room_for_the_run(fake: Fake, repo: Path, tmp_path: Path) -> None:
    fake.state["vms"]["ava-r1"]["running"] = True
    fake.save()

    status, _result, _root = _run(fake, repo, tmp_path)

    assert status == 0
    assert fake.vms()["ava-r1"]["running"] is True  # a VM the run did not start is left alone


def test_a_golden_image_that_is_running_or_missing_is_refused(
    fake: Fake, repo: Path, tmp_path: Path
) -> None:
    fake.state["vms"][GOLDEN]["running"] = True
    fake.save()
    status, result, _root = _run(fake, repo, tmp_path)
    assert status == 1 and "clone only from a stopped image" in result["error"]
    assert not fake.commands("clone")

    del fake.state["vms"][GOLDEN]
    fake.save()
    status = tart_run.run(
        "HEAD", tmp_path / "evidence2", repo=repo, golden=GOLDEN, tart=fake.tart()
    )
    assert status == 1 and not fake.commands("clone")


def test_only_a_run_vm_can_be_deleted(fake: Fake) -> None:
    for name in (GOLDEN, *EARLIER):
        with pytest.raises(ValueError, match="not a verification run VM"):
            fake.tart().delete_run_vm(name)
    assert not fake.commands("delete")
    assert set(fake.vms()) == {GOLDEN, *EARLIER}


# ------------------------------------------------------------------------ the boundary


def test_the_only_share_is_the_commit_export_read_only_and_nothing_leaks(
    fake: Fake, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".ava/source").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    status, _result, _root = _run(fake, repo, tmp_path)

    assert status == 0
    (booted,) = fake.commands("run")
    shares = [arg for arg in booted if arg.startswith("--dir=")]
    assert len(shares) == 1 and shares[0].endswith(":ro")
    source = Path(shares[0].removeprefix("--dir=").split(":")[1])
    assert shares[0].startswith(f"--dir={tart_vm.SHARE_NAME}:")
    assert not source.is_relative_to(repo) and not source.is_relative_to(home)
    assert {"--no-clipboard", "--no-audio", "--no-usb-accessories", "--no-graphics"} <= set(booted)
    everything = json.dumps(fake.calls())
    assert str(home) not in everything
    assert str(repo) not in everything  # the host repository is fetched from, never mounted
    assert not source.exists()  # the export is a temporary directory, removed with the run


def test_the_export_holds_only_the_commit_and_no_remote(repo: Path, tmp_path: Path) -> None:
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "other", "HEAD~1")
    _git(repo, "remote", "add", "origin", "git@example.test:private/repo.git")
    destination = tmp_path / "export"
    destination.mkdir()

    tart_vm.export_commit(repo, commit, destination)

    bare = destination / "source.git"
    assert _git(bare, "for-each-ref", "--format=%(refname) %(objectname)") == (
        f"refs/heads/{tart_vm.EXPORT_BRANCH} {commit}"
    )
    assert "remote" not in (bare / "config").read_text()
    assert "example.test" not in (bare / "config").read_text()
    assert _git(bare, "rev-list", "--all", "--count") == "2"


@pytest.mark.parametrize(
    "relative", [".", "..", ".ava", ".ava/source", ".tart", "Library/Keychains"]
)
def test_a_share_of_host_state_is_refused_before_tart_is_asked(
    relative: str, fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    for name in (".ava/source", ".tart", "Library/Keychains"):
        (home / name).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ValueError, match="refusing to mount host state"):
        fake.tart().boot_argv("vm", share=home / relative, graphics=False)
    assert not fake.calls()


def test_the_start_profile_and_the_observer_are_what_the_guest_receives(
    fake: Fake, repo: Path, tmp_path: Path
) -> None:
    _run(fake, repo, tmp_path)

    uploads = [call["stdin"] for call in fake.calls() if call["stdin"] is not None]
    assert uploads == [boundary.profile_text(), (tart_run.RECIPE / "observe.py").read_text()]


def test_every_guest_script_goes_through_the_guest_agent_never_the_host_shell(
    fake: Fake, repo: Path, tmp_path: Path
) -> None:
    _run(fake, repo, tmp_path)

    scripts = [call["argv"] for call in fake.calls() if "-c" in call["argv"]]
    assert scripts, "the run must have executed guest scripts"
    for argv in scripts:
        assert argv[0] == "exec" and argv[argv.index("-c") - 1] == "bash"
    # The one destructive guest command removes the checkout, inside the guest.
    destructive = [a for argv in scripts for a in argv if "rm -rf" in a]
    assert {
        line.strip() for script in destructive for line in script.splitlines() if "rm -rf" in line
    } == {'rm -rf "$HOME/.ava/source"'}


def test_init_and_start_are_the_commands_the_cli_accepts() -> None:
    """The guest runs `ava init` (machine, capabilities, profile) and then `ava start` with
    the service selection only, the same split the CLI parsers enforce."""
    from cli.parsers import build_parser

    def command(script: str) -> list[str]:
        return shlex.split(script.splitlines()[-1].split(" && ", 1)[1])

    init, start = command(tart_run.INIT), command(tart_run.START)
    parser = build_parser()
    parsed = parser.parse_args(init[1:])
    assert parsed.machine_name == boundary.MACHINE_NAME and parsed.serve_gateway is True
    assert parsed.config_file == "$HOME/verify/profile.env"
    assert parser.parse_args(start[1:]).only_service == list(boundary.SERVICES)


# ----------------------------------------------------------------------------- evidence


def test_the_guest_logs_are_unpacked_only_as_plain_files_under_the_evidence_directory(
    tmp_path: Path,
) -> None:
    tart_run.unpack_logs(base64.b64decode(_logs_archive({"logs/a.log": "a"})), tmp_path / "out")
    assert (tmp_path / "out/logs/a.log").read_text() == "a"

    escape = base64.b64decode(_logs_archive({"../escape.log": "x"}))
    with pytest.raises(ValueError, match="refusing archive member"):
        tart_run.unpack_logs(escape, tmp_path / "out2")
    assert not (tmp_path / "escape.log").exists()

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        link = tarfile.TarInfo("logs/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    with pytest.raises(ValueError, match="refusing archive member"):
        tart_run.unpack_logs(buffer.getvalue(), tmp_path / "out3")


def test_every_step_the_run_records_has_a_bound() -> None:
    steps = {"clone", "keychain", "prepare-source", "toolchain", "python", "profile-upload",
             "observer-upload", "init", "retire-golden-helper", "start", "observe", "snapshot"}  # fmt: skip
    assert steps == set(tart_run.TIMEOUTS)
    assert all(seconds > 0 for seconds in tart_run.TIMEOUTS.values())
