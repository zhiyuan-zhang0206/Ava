"""Collection diagnostics own their worker and preserve the command's verdict."""

import faulthandler
import sys
import threading
from pathlib import Path
from typing import TextIO

import pytest
import yaml

from scripts.ci.collection_tracebacks import periodic_tracebacks

ROOT = Path(__file__).resolve().parents[3]


def assert_worker_stopped() -> None:
    assert not any(t.name == "collection-tracebacks" for t in threading.enumerate())


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_invalid_interval_starts_no_worker(tmp_path: Path, interval: float) -> None:
    with (
        (tmp_path / "stacks.log").open("w") as stacks,
        pytest.raises(ValueError, match="finite and positive"),
        periodic_tracebacks(stacks, interval=interval),
    ):
        pytest.fail("Invalid interval reached collection")
    assert_worker_stopped()


def test_early_completion_cancels_wait_without_dumping(tmp_path: Path) -> None:
    output = tmp_path / "stacks.log"
    enabled = faulthandler.is_enabled()
    with output.open("w") as stacks, periodic_tracebacks(stacks, interval=600):
        pass
    assert output.read_text() == ""
    assert faulthandler.is_enabled() is enabled
    assert_worker_stopped()


def test_repeating_dumps_stop_before_file_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    twice = threading.Event()
    calls: list[tuple[int, bool]] = []

    def dump(*, file: TextIO, all_threads: bool) -> None:
        assert not file.closed
        calls.append((threading.get_ident(), all_threads))
        if len(calls) >= 2:
            twice.set()

    monkeypatch.setattr(faulthandler, "dump_traceback", dump)
    with (tmp_path / "stacks.log").open("w") as stacks:
        with periodic_tracebacks(stacks, interval=0.001):
            assert twice.wait(2)
        count = len(calls)
        assert_worker_stopped()
    assert len(calls) == count
    assert all(tid != threading.get_ident() and all_threads for tid, all_threads in calls)


def test_diagnostic_failure_reaches_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempted = threading.Event()

    def dump(*, file: TextIO, all_threads: bool) -> None:
        attempted.set()
        raise OSError("Diagnostic output unavailable")

    monkeypatch.setattr(faulthandler, "dump_traceback", dump)
    with (
        (tmp_path / "stacks.log").open("w") as stacks,
        pytest.raises(RuntimeError, match="diagnostic failed") as failure,
        periodic_tracebacks(stacks, interval=0.001),
    ):
        assert attempted.wait(2)
    assert isinstance(failure.value.__cause__, OSError)
    assert_worker_stopped()


def run_isolated(pytester: pytest.Pytester, code: str) -> pytest.RunResult:
    # Pytester's owner supplies the cwd, output capture and hard timeout kill/wait.
    assert Path.cwd() == pytester.path
    return pytester.run(
        sys.executable,
        "-I",
        "-S",
        "-c",
        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + code,
        timeout=3,
    )


@pytest.mark.parametrize("exit_code", [0, 4])
def test_collection_exit_preserved_and_worker_joined(
    tmp_path: Path, pytester: pytest.Pytester, exit_code: int
) -> None:
    result = run_isolated(
        pytester,
        "import threading\n"
        "from scripts.ci.collection_tracebacks import periodic_tracebacks\n"
        f"with open({str(tmp_path / 'stacks.log')!r}, 'w') as stacks:\n"
        "    with periodic_tracebacks(stacks):\n"
        f"        raise SystemExit({exit_code})\n",
    )
    assert result.ret == exit_code, str(result.stderr)


def test_frame_churn_finishes_with_complete_tracebacks(
    tmp_path: Path, pytester: pytest.Pytester
) -> None:
    output = tmp_path / "stacks.log"
    result = run_isolated(
        pytester,
        "import threading, time\n"
        "from scripts.ci.collection_tracebacks import periodic_tracebacks\n"
        "def recurse(depth):\n"
        "    a, b, c = 1, 2, 3\n"
        "    return recurse(depth - 1) if depth else a + b + c\n"
        "def churn():\n"
        "    end = time.monotonic() + 0.25\n"
        "    while time.monotonic() < end:\n"
        "        assert recurse(123) == 6\n"
        f"with open({str(output)!r}, 'w') as stacks:\n"
        "    with periodic_tracebacks(stacks, interval=0.001):\n"
        "        worker = threading.Thread(target=churn)\n"
        "        worker.start()\n"
        "        worker.join()\n"
        "assert not any(t.name == 'collection-tracebacks' for t in threading.enumerate())\n",
    )
    assert result.ret == 0, str(result.stderr)
    trace = output.read_text()
    assert "Periodic traceback" in trace
    assert 'File "' in trace and ", line " in trace
    assert "churn" in trace


@pytest.mark.parametrize("workflow", ["ci.yml", "file-shard-proof.yml"])
@pytest.mark.parametrize("exit_code", [0, 4])
def test_workflow_planner_preserves_verdict(
    pytester: pytest.Pytester, workflow: str, exit_code: int
) -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
    script = next(
        step["run"]
        for job in jobs.values()
        for step in job.get("steps", [])
        if "periodic_tracebacks" in step.get("run", "")
    )
    body = script.split("<<'PY'", 1)[1].split("\n", 1)[1].split("\nPY", 1)[0]
    (pytester.path / "tmp/file-shards").mkdir(parents=True)
    result = run_isolated(
        pytester,
        "import types\n"
        "pytest = types.ModuleType('pytest')\n"
        "def main(args):\n"
        "    assert '--collect-only' in args\n"
        "    print('planner called')\n"
        f"    return {exit_code}\n"
        "pytest.main = main\n"
        "sys.modules['pytest'] = pytest\n"
        f"exec(compile({body!r}, 'workflow-planner', 'exec'))\n",
    )
    assert result.ret == exit_code, str(result.stderr)
    assert "planner called" in str(result.stdout)
    assert len(list((pytester.path / "tmp").rglob("*-stacks.log"))) == 1
