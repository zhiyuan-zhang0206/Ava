"""`Host` and `Platform`: the real host answers for this machine, a hand-built one for a test."""

from __future__ import annotations

import dataclasses
import os
import platform
import shutil
import subprocess
import sys
from typing import Any, cast

import pytest

from base.host import proc
from base.host.system.hostport import Host, Platform
from base.host.tests.fake_commands import FakeCommands, Reply


def test_the_system_host_is_this_machine() -> None:
    host = Host.system()
    assert host.run is proc.run_bounded
    assert host.which is shutil.which
    assert host.uid == os.getuid()
    assert host.platform.executable == sys.executable
    assert host.platform.machine == platform.machine()
    assert host.platform.system == ("darwin" if sys.platform == "darwin" else "linux")


def test_the_system_host_runs_a_real_command() -> None:
    result = Host.system().run(
        [sys.executable, "-c", "print('hi')"], timeout=60, capture_output=True, text=True
    )
    assert (result.returncode, result.stdout) == (0, "hi\n")


def test_the_system_host_bounds_a_command_by_its_timeout() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        Host.system().run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)


@pytest.mark.parametrize(
    ("sys_platform", "system"),
    [("darwin", "darwin"), ("linux", "linux"), ("linux2", "linux")],
)
def test_a_supported_sys_platform_reduces_to_linux_or_darwin(
    sys_platform: str, system: str
) -> None:
    built = Platform.from_sys_platform(sys_platform, machine="arm64", executable="/usr/bin/python")
    assert built.system == system
    assert built == Platform(cast("Any", system), "arm64", "/usr/bin/python")


@pytest.mark.parametrize("sys_platform", ["win32", "cygwin", "freebsd14", ""])
def test_an_unsupported_sys_platform_is_refused(sys_platform: str) -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        Platform.from_sys_platform(sys_platform, machine="x86_64", executable="/usr/bin/python")


@pytest.mark.parametrize(
    ("system", "machine", "executable", "message"),
    [
        ("windows", "x86_64", "/usr/bin/python", "system"),
        ("linux", "", "/usr/bin/python", "machine"),
        ("linux", "x86_64", "", "executable"),
    ],
)
def test_a_platform_rejects_values_it_cannot_describe(
    system: str, machine: str, executable: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Platform(cast("Any", system), machine, executable)


def test_a_host_is_immutable_and_has_exactly_the_four_reviewed_fields() -> None:
    host = Host(
        run=FakeCommands(),
        which=lambda _name: None,
        platform=Platform("linux", "x86_64", "/usr/bin/python"),
        uid=1000,
    )
    assert [f.name for f in dataclasses.fields(Host)] == ["run", "which", "platform", "uid"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        host.__setattr__("uid", 1)


def test_a_test_builds_the_host_it_wants() -> None:
    commands = FakeCommands()
    commands.on("lsof", Reply(stdout="p1109\n"))
    host = Host(
        run=commands,
        which=lambda name: "/opt/bin/lsof" if name == "lsof" else None,
        platform=Platform("darwin", "arm64", "/usr/bin/python"),
        uid=501,
    )
    assert host.which("lsof") == "/opt/bin/lsof"
    assert host.which("nothing") is None
    done = host.run(["lsof", "-nP"], timeout=10, capture_output=True, text=True)
    assert done.stdout == "p1109\n"
    assert commands.argvs == [("lsof", "-nP")]
