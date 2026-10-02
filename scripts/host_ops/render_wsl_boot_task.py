"""Render an opt-in WSL host boot task; never register or start a task.

The Windows account owns the distribution. The Linux account owns Ava's
existing cron/``ava boot`` chain. This task only holds the distribution open.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import PureWindowsPath
from xml.sax.saxutils import escape


def _identity(value: str, label: str) -> str:
    if not value or value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} must be nonempty and contain no control characters")
    if value.startswith("-"):
        raise ValueError(f"{label} must not start with '-'")
    return value


def render_task(
    *, distribution: str, linux_user: str, windows_user: str, wsl_executable: str
) -> str:
    """Return password-free S4U XML for the explicitly named distro owner.

    A repeating boot trigger retries a failed or normally exited WSL client;
    IgnoreNew preserves one live anchor without a custom supervisor loop.
    """
    distribution = _identity(distribution, "distribution")
    linux_user = _identity(linux_user, "Linux user")
    windows_user = _identity(windows_user, "Windows user")
    if "\\" not in windows_user or not all(windows_user.split("\\")):
        raise ValueError("Windows user must be qualified as MACHINE\\user or DOMAIN\\user")
    if windows_user.casefold().startswith("nt authority\\"):
        raise ValueError("Windows user must own the distribution, not be a service account")
    executable = PureWindowsPath(_identity(wsl_executable, "WSL executable"))
    if (
        not executable.is_absolute()
        or len(executable.drive) != 2
        or executable.name.casefold() != "wsl.exe"
    ):
        raise ValueError("WSL executable must be an absolute local Windows path to wsl.exe")
    arguments = subprocess.list2cmdline(
        [
            "--distribution",
            distribution,
            "--user",
            linux_user,
            "--exec",
            "/usr/bin/sleep",
            "infinity",
        ]
    )
    # The same XML travels as stdout bytes and as PowerShell's UTF-16 BSTR.
    xml = f"""<?xml version="1.0"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Keep WSL {escape(distribution)} running for its existing Ava boot chain.</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Owner">
      <UserId>{escape(windows_user)}</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Owner">
    <Exec>
      <Command>{escape(str(executable))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""
    # Numeric references preserve Unicode identities through legacy Windows pipes.
    return xml.encode("ascii", errors="xmlcharrefreplace").decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--linux-user", required=True)
    parser.add_argument("--windows-user", required=True)
    parser.add_argument("--wsl-executable", required=True)
    args = parser.parse_args()
    try:
        xml = render_task(**vars(args))
    except ValueError as exc:
        parser.error(str(exc))
    print(xml, end="")


if __name__ == "__main__":
    main()
