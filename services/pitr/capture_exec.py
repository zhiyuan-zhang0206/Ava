"""Bind durable process identity before execing pg_basebackup."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import psutil


def _publish(path: Path, value: dict[str, object]) -> None:
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        staged.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staged.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True, type=Path)
    parser.add_argument("--deadline", required=True, type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command: list[str] = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ValueError("capture exec requires a command")
    process = psutil.Process()
    _publish(
        args.owner,
        {
            "state": "running",
            "pid": process.pid,
            "pgid": os.getpgrp(),
            "created_at": process.create_time(),
            "deadline": args.deadline,
            "expected_token": command[0],
        },
    )
    os.execv(command[0], command)  # noqa: S606


if __name__ == "__main__":
    main()
