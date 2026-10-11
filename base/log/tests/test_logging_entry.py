"""Fresh executable preludes are explicit; imports and embedded calls keep sinks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]


def run_probe(code: str, home: Path) -> dict[str, object]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env["AVA_HOME"] = str(home)
    child = subprocess.run(  # noqa: S603 — this interpreter, fixed proof, private home
        [sys.executable, "-I", "-B", "-c", code, str(_ROOT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout.splitlines()[-1])


def test_import_and_embedded_dispatch_preserve_caller_logging(tmp_path: Path) -> None:
    observed = run_probe(
        """
import json, sys
sys.path.insert(0, sys.argv[1])
from loguru import logger
logger.remove()
records = []
sink = logger.add(lambda message: records.append(message.record["message"]), diagnose=False)
logger.configure(extra={"caller": "retained"})
import base.log
from cli.main import main
logger.info("after import")
for _ in range(2):
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
logger.info("after embedded dispatch")
logger.remove(sink)
print(json.dumps({"records": records}))
""",
        tmp_path / "home",
    )
    assert observed["records"] == ["after import", "after embedded dispatch"]


def test_fresh_entry_prepares_before_dispatch_without_telemetry(tmp_path: Path) -> None:
    observed = run_probe(
        """
import json, sys
sys.path.insert(0, sys.argv[1])
from loguru import logger
records = []
sink = logger.add(lambda message: records.append(message.record["message"]), diagnose=False)
from cli import main
# A public dispatch stub observes the actual executable prelude.
def dispatch():
    logger.warning("must be quiet before configuration")
    return 17
main.main = dispatch
result = main.entry()
print(json.dumps({"records": records, "result": result,
    "otel": sorted(name for name in sys.modules if name.startswith("opentelemetry"))}))
""",
        tmp_path / "home",
    )
    assert observed == {"records": [], "result": 17, "otel": []}


@pytest.mark.parametrize("role", ["gateway", "cli", "subprocess"])
def test_role_initialization_keeps_caller_sink_and_first_attribution(
    role: str, tmp_path: Path
) -> None:
    observed = run_probe(
        """
import json, sys
sys.path.insert(0, sys.argv[1])
from base.log import logger, prepare_process_logging
prepare_process_logging()
records = []
agent_ids = []
def observe(message):
    records.append(message.record["message"])
    agent_ids.append(message.record["extra"]["agent_id"])
sink = logger.add(observe, diagnose=False)
from base.log.process import init_cli_process, init_gateway_process, init_subprocess_logger
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.delivery.pipeline import EventPipeline
rows = []
pipeline = EventPipeline(writer=rows.extend)
inputs = {"producer": lambda: pipeline, "machine_reader": lambda: "entry-test"}
role = ROLE
if role == "subprocess":
    init_subprocess_logger(agent_id=7)
elif role == "cli":
    init_cli_process(name="entry-test", **inputs)
else:
    init_gateway_process(name="entry-test", image=LoadedCommit(__import__("pathlib").Path(sys.argv[1]), None), **inputs)
init_cli_process(name="ignored", **inputs)
init_subprocess_logger(agent_id=99)
init_gateway_process(name="ignored", image=LoadedCommit(__import__("pathlib").Path(sys.argv[1]), None), **inputs)
logger.info("after repeated initialization", event="label_change")
result = pipeline.stop(timeout=2)
logger.remove(sink)
print(json.dumps({"marker_count": records.count("after repeated initialization"),
    "agent": agent_ids[-1], "drained": result.status.value}))
""".replace("ROLE", repr(role)),
        tmp_path / "home",
    )
    assert observed["marker_count"] == 1
    assert observed["agent"] == ("7" if role == "subprocess" else "-")
    assert observed["drained"] == "completed"


@pytest.mark.parametrize("executable", ["console", "cli", "cli.main"])
def test_fresh_cli_executables_stay_quiet(executable: str, tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    home = tmp_path / "home"
    env["AVA_HOME"] = str(home)
    argv = (
        [str(_ROOT / ".venv/bin/ava")]
        if executable == "console"
        else [sys.executable, "-m", executable]
    )
    child = subprocess.run(  # noqa: S603 — fixed actual entry and help arguments
        [*argv, "--help"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert "usage:" in child.stdout
    assert child.stderr == ""
    assert not (home / "logs").exists()


def test_no_agent_child_prepares_before_main_without_telemetry(tmp_path: Path) -> None:
    observed = run_probe(
        """
import json, runpy, sys
sys.path.insert(0, sys.argv[1])
from loguru import logger
records = []
sink = logger.add(lambda message: records.append(message.record["message"]), diagnose=False)
try:
    runpy.run_module("agent.execution.child", run_name="__main__")
except SystemExit as exc:
    assert exc.code == 2
logger.warning("must stay out of exec output after the no-request refusal")
print(json.dumps({"records": records, "loaded": sorted(name for name in sys.modules
    if name.startswith("opentelemetry") or name == "base.telemetry")}))
""",
        tmp_path / "home",
    )
    assert observed == {"records": [], "loaded": []}
