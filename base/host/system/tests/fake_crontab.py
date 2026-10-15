from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any


class FakeCrontab:
    def __init__(self, table: str = "") -> None:
        self.table = table
        self.read_returncode = 0
        self.read_stderr = ""
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
        input: bytes | str | None = None,
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
