"""CI-only fixed owner entry from an installed image with the checkout absent."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psutil

import agent.exec_domain_owner
from agent.graph._exec_protocol import write_request
from shared.exec_owner_protocol import (
    OwnerClosed,
    OwnerContext,
    OwnerControl,
    OwnerReady,
    publish_owner_message,
)
from shared.incarnation_resources import ExecAllocation


def main() -> None:  # noqa: PLR0915
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    prefix = Path(sys.prefix).resolve()
    loaded = Path(agent.exec_domain_owner.__file__).resolve()
    if not loaded.is_relative_to(prefix):
        raise AssertionError("owner entry did not load from the installed image")
    identity = uuid4()
    request = (root / f"req-{identity.hex}.json").resolve()
    active = (root / "active").resolve()
    result_path = (root / "result.json").resolve()
    # Offline standalone execution without live PostgreSQL uses agent_id=None
    # so exec_child runs user code without attempting DB connection boot (task #2679).
    write_request(
        request,
        code=f"from pathlib import Path\nimport time\nPath({str(active)!r}).touch()\ntime.sleep(120)",
        agent_id=None,
        timeout_s=60,
        state=None,
    )
    context = OwnerContext(
        agent_id=1,
        generation=uuid4(),
        runtime_owner=uuid4(),
        request_path=request,
        result_path=result_path,
        allocation=ExecAllocation(
            request=identity,
            domain=uuid4(),
            request_digest=hashlib.sha256(request.read_bytes()).hexdigest(),
            deadline=datetime.now(UTC) + timedelta(seconds=120),
        ),
    )
    path = (root / "owner.json").resolve()
    publish_owner_message(path, context)
    started = time.monotonic()
    log_file = root / "owner.log"
    with log_file.open("wb") as output:
        child = subprocess.Popen(  # noqa: S603 — exact retained interpreter and fixed isolated module.
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                "-m",
                "agent.exec_domain_owner",
                "--context",
                str(path),
            ],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.STDOUT,
            cwd=root,
            env=dict(
                os.environ,
                PYTHONPATH=str(root / "poison"),
                AVA_EXEC_REQUEST_FILE=str(request),
                AVA_EXEC_RESULT_FILE=str(context.result_path),
            ),
            close_fds=True,
        )
        try:
            while not path.with_suffix(".ready").exists():
                if child.poll() is not None or time.monotonic() - started > 60:
                    output.flush()
                    log_text = log_file.read_text(errors="replace") if log_file.exists() else ""
                    raise AssertionError(
                        f"installed owner failed before handshake (exit={child.poll()}):\n{log_text}"
                    )
                time.sleep(0.02)
            ready = OwnerReady.model_validate_json(path.with_suffix(".ready").read_bytes())
            if child.stdin is None or ready.allocation.owner_process is None:
                raise AssertionError("installed owner identity/control pipe missing")
            native = psutil.Process(ready.allocation.owner_process.pid)
            rss = native.memory_info().rss
            child.stdin.write(
                OwnerControl(request=identity, domain=context.allocation.domain, action="permit")
                .model_dump_json()
                .encode()
                + b"\n"
            )
            child.stdin.flush()

            permit_started = time.monotonic()
            while not active.exists():
                if child.poll() is not None or time.monotonic() - permit_started > 80:
                    output.flush()
                    log_text = log_file.read_text(errors="replace") if log_file.exists() else ""
                    res_text = (
                        result_path.read_text(errors="replace") if result_path.exists() else ""
                    )
                    raise AssertionError(
                        f"installed exec never reached real user code (exit={child.poll()}):\nowner.log:\n{log_text}\nresult.json:\n{res_text}"
                    )
                time.sleep(0.02)
            child.stdin.close()
            if child.wait(timeout=15) != 0:
                output.flush()
                log_text = log_file.read_text(errors="replace") if log_file.exists() else ""
                raise AssertionError(
                    f"installed owner failed its domain closure (exit={child.returncode}):\n{log_text}"
                )
            closed = OwnerClosed.model_validate_json(path.with_suffix(".closed").read_bytes())
            if closed.allocation != ready.allocation or closed.reason != "host_eof":
                raise AssertionError("installed terminal receipt differs from its allocation")
            (root.parent / "exec-owner-installed.json").write_text(
                json.dumps(
                    {
                        "loadedModule": str(loaded),
                        "prefix": str(prefix),
                        "sourceAbsent": True,
                        "actualUserCode": True,
                        "exactClosedReceipt": True,
                        "elapsedMs": (time.monotonic() - started) * 1000,
                        "ownerRssBytes": rss,
                        "productionActivation": False,
                    },
                    sort_keys=True,
                )
            )
        finally:
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
            if child.poll() is None:
                child.wait(timeout=70)


if __name__ == "__main__":
    main()
