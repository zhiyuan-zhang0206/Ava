"""The finite executor's own records reach the logs its native adapter keeps.

The executor runs as `python -m cli.release_transition.execute`. Importing
`shared.log` drops loguru's default handler, so a process that never adds a
sink discards every record it writes: the traceback a routed phase failure
keeps only in the log, the undelivered-alert error, missed heartbeat and
lease rounds, the listener's refusal reasons. This runs a real child process,
without the in-process `loguru_records` fixture (which adds a sink the
executor never has).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from cli.release_fleet.policy import FleetPolicy
from cli.release_fleet.request import FleetRequest
from cli.release_transition.journal import create, read_operation
from cli.release_transition.request import ReleaseRef
from tests.lifecycle.release_fleet.test_coordinator import _WHEN

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The executor's own `main`, with only the native admission (image
# verification, launch receipt, heartbeat) replaced by a fleet-of-one
# coordinator run whose quiescing effect fails: a routed failure before the
# fence, which aborts and keeps its traceback only in the log.
_CHILD = """
from cli.release_transition import execute
from cli.release_transition.journal import exclusive
from tests.lifecycle.release_fleet.fakes import OffDutyGateway, drive


class QuiesceFails(OffDutyGateway):
    def quiesce(self, operation):
        raise RuntimeError("injected quiesce failure")


def run(path):
    with exclusive(path) as journal:
        drive(journal, QuiesceFails(journal.operation.request))


execute.execute = run
raise SystemExit(execute.main())
"""


def _journal(home: Path) -> Path:
    previous = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    candidate = previous.model_copy(update={"artifact_digest": "e" * 64, "source_commit": "9" * 40})
    (home / "releases").mkdir(parents=True)
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": previous.artifact_digest,
                "manifest_digest": previous.manifest_digest,
            }
        )
    )
    request = FleetRequest(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "registry.json"),
        created_at=_WHEN,
        machine="test",
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest="f" * 64,
        policy=FleetPolicy(watch_s=120),
    )
    create(request)
    return request.path


def test_a_routed_failures_traceback_reaches_the_executors_stderr_and_log_file(
    tmp_path: Path,
) -> None:
    home = tmp_path.resolve() / "home"
    path = _journal(home)
    child = subprocess.run(  # noqa: S603 — this interpreter, fixed code, a private journal
        [sys.executable, "-c", _CHILD, "--operation", str(path)],
        cwd=_REPO_ROOT,
        env={**os.environ, "AVA_HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert child.returncode == 0, child.stderr
    final = read_operation(path)
    assert final.fleet is not None and final.fleet.outcome == "aborted"

    # stderr is what the systemd journal and launchd's `stderr.log` keep.
    stderr = _ANSI.sub("", child.stderr)
    assert "[release-fleet] quiescing failed: RuntimeError: injected quiesce failure" in stderr
    assert "Traceback (most recent call last)" in stderr
    assert "RuntimeError: injected quiesce failure" in stderr.split("Traceback", 1)[1]

    # The per-process log file under the home outlives the native attempt.
    records = [
        json.loads(line)
        for line in (home / "logs/release-executor.log").read_text().splitlines()
        if line.strip()
    ]
    [failure] = [r for r in records if "quiescing failed" in json.dumps(r)]
    assert "injected quiesce failure" in json.dumps(failure)
