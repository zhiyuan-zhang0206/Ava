"""`FakeCrontab`: the user's crontab behind `crontab -l` and `crontab -`, as a `CommandRunner`.

Unlike the scripted `FakeCommands`, it keeps state: a write replaces the table the next read
returns, so a test can register a job, register it again and unregister it against one table.
Its failures are attributes a test sets: a read that fails with a message, a write that fails
with a message. Any other command fails the test.

    crontab = FakeCrontab("0 3 * * * backup\\n")
    host = fake_host(crontab, platform=LINUX_X86, programs={"crontab": "/usr/bin/crontab"})
    ...
    assert crontab.table.splitlines() == ["0 3 * * * backup", "40 4 * * * ..."]
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any


class FakeCrontab:
    """An in-memory crontab. `table` is what `crontab -l` prints; `writes` is every body written."""

    def __init__(self, table: str = "") -> None:
        self.table = table
        self.read_returncode = 0
        self.read_stderr = ""
        # A message here makes every `crontab -` fail with it (and leaves the table alone).
        self.write_failure: str | None = None
        self.writes: list[str] = []
        self.argvs: list[tuple[str, ...]] = []

    @property
    def lines(self) -> list[str]:
        return self.table.splitlines()

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,  # mirrors subprocess.run's own parameter name
        **_popen_kwargs: object,
    ) -> subprocess.CompletedProcess[Any]:
        command = tuple(argv)
        self.argvs.append(command)
        if command == ("crontab", "-l"):
            return subprocess.CompletedProcess(
                list(command), self.read_returncode, self.table, self.read_stderr
            )
        if command == ("crontab", "-"):
            if self.write_failure is not None:
                return subprocess.CompletedProcess(list(command), 1, "", self.write_failure)
            self.table = str(input)
            self.writes.append(self.table)
            return subprocess.CompletedProcess(list(command), 0, "", "")
        raise AssertionError(f"unscripted command: {list(command)}")
