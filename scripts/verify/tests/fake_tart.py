"""A stand-in for the `tart` binary, so the macOS recipes run offline.

The tests copy this file to `<dir>/tart` behind a shebang and point `FAKE_TART_HOME` at
a directory holding `state.json`:

    {"vms": {"<name>": {"running": false}}, "fail_on": ["<script substring>"],
     "observer": "<observer.json text>", "logs_b64": "<base64 of a tar.gz>"}

It answers `list`, `clone`, `set`, `run`, `stop`, `delete` and `exec` against that state,
appends every call (argv, and stdin when `exec -i` was used) to `calls.log`, and makes
`exec` fail when the script contains one of the `fail_on` strings.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HOME = Path(os.environ["FAKE_TART_HOME"])


def _load() -> dict[str, Any]:
    return json.loads((HOME / "state.json").read_text())


def _save(state: dict[str, Any]) -> None:
    (HOME / "state.json").write_text(json.dumps(state))


def _record(argv: list[str], stdin: str | None) -> None:
    with (HOME / "calls.log").open("a") as log:
        log.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")


def _exec(state: dict[str, Any], rest: list[str]) -> int:
    """`rest` is what follows `exec` (and its `-i`): the VM name, then the command."""
    name = rest[0]
    if not state["vms"].get(name, {}).get("running"):
        return 1
    if rest[1:] == ["true"]:
        return 0
    script = rest[rest.index("-c") + 1]
    if any(marker in script for marker in state["fail_on"]):
        print("fake tart: scripted failure", file=sys.stderr)
        return 1
    if "cat $HOME/verify/observer.json" in script:
        print(state["observer"])
    elif "base64" in script:
        print(state["logs_b64"])
    return 0


def main() -> int:
    argv = sys.argv[1:]
    stdin = sys.stdin.read() if argv[:2] == ["exec", "-i"] else None
    _record(argv, stdin)
    state = _load()
    vms: dict[str, dict[str, bool]] = state["vms"]
    command = argv[0]
    if command == "--version":
        print("9.9.9-fake")
    elif command == "list":
        rows = [{"Source": "local", "Name": n, "Running": v["running"]} for n, v in vms.items()]
        rows.append({"Source": "OCI", "Name": "ghcr.io/example/image@sha256:0", "Running": False})
        print(json.dumps(rows))
    elif command == "clone":
        if argv[1] not in vms:
            return 1
        vms[argv[2]] = {"running": False}
        _save(state)
    elif command == "run":
        vms[argv[-1]]["running"] = True
        _save(state)
        while _load()["vms"].get(argv[-1], {}).get("running"):
            time.sleep(0.05)
    elif command == "stop":
        vms[argv[1]]["running"] = False
        _save(state)
    elif command == "delete":
        if vms[argv[1]]["running"]:
            return 1
        del vms[argv[1]]
        _save(state)
    elif command == "exec":
        return _exec(state, argv[1:] if stdin is None else argv[2:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
