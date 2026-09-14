"""Execute the real label-gate shell with a mock GitHub API; never call the network."""

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "qa-approved-gate.yml"
JOB = "qa-approved-gate"
STEP = "Check QA approval label"
REPO = "owner/repository"
QA_LABEL = {"name": "qa-approved"}
OTHER_LABEL = {"name": "other"}


def gate_script() -> str:
    """The step's shell, read from the real workflow file."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = [s for s in workflow["jobs"][JOB]["steps"] if s.get("name") == STEP]
    assert len(steps) == 1
    return steps[0]["run"]


def run_gate(
    tmp_path: Path,
    *,
    live_labels: list[dict[str, str]],
    payload_labels: list[dict[str, str]] | None = None,
    head_ref: str = "feature",
    api_failure: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run the step with a stubbed `gh`; return (result, gh call log).

    `live_labels` is the label set the API returns at evaluation time;
    `payload_labels` is the stale event payload a payload-based check would
    have read instead."""
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps({"pull_request": {"number": 42, "labels": payload_labels or []}})
    )
    call_log = tmp_path / "calls.jsonl"
    mock = tmp_path / "gh"
    mock.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['CALL_LOG'], 'a') as log: log.write(json.dumps(args) + '\\n')\n"
        "if os.environ.get('API_FAIL') == '1': sys.exit(1)\n"
        "program = args[args.index('--jq') + 1]\n"
        "done = subprocess.run(\n"
        "    ['jq', '-r', program], input=os.environ['LIVE_LABELS'],\n"
        "    capture_output=True, text=True,\n"
        ")\n"
        "sys.stdout.write(done.stdout)\n"
        "sys.stderr.write(done.stderr)\n"
        "sys.exit(done.returncode)\n"
    )
    mock.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.defpath}",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_PATH": str(event_path),
        "LIVE_LABELS": json.dumps(live_labels),
        "CALL_LOG": str(call_log),
        "API_FAIL": "1" if api_failure else "0",
        "HEAD_REF": head_ref,
        "PR_NUMBER": "42",
    }
    result = subprocess.run(  # noqa: S603 — checked-in shell; gh is an isolated mock
        ["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", gate_script()],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        cwd=str(tmp_path),
    )
    calls = (
        [json.loads(line) for line in call_log.read_text().splitlines()]
        if call_log.exists()
        else []
    )
    return result, calls


def test_late_run_with_stale_payload_passes_when_label_present(tmp_path: Path) -> None:
    """#2444: a run queued before `labeled` carries a payload without the
    label; once the label is present, the live read must pass it."""
    result, calls = run_gate(tmp_path, live_labels=[QA_LABEL], payload_labels=[])
    assert result.returncode == 0, result.stderr
    assert calls, "the gate must read the labels live"


def test_removed_label_is_red_even_when_the_payload_still_lists_it(tmp_path: Path) -> None:
    """The stale-payload pass: a payload snapshot still carries the label
    after it was removed; the live read must reject it."""
    result, calls = run_gate(tmp_path, live_labels=[OTHER_LABEL], payload_labels=[QA_LABEL])
    assert result.returncode == 1, result.stdout
    assert calls, "the gate must read the labels live"


def test_unlabeled_pr_is_red(tmp_path: Path) -> None:
    result, _ = run_gate(tmp_path, live_labels=[])
    assert result.returncode == 1


def test_label_read_is_the_issue_labels_endpoint(tmp_path: Path) -> None:
    result, calls = run_gate(tmp_path, live_labels=[QA_LABEL])
    assert result.returncode == 0, result.stderr
    assert calls[0][:2] == ["api", f"repos/{REPO}/issues/42/labels"]


def test_api_failure_fails_closed(tmp_path: Path) -> None:
    result, _ = run_gate(tmp_path, live_labels=[QA_LABEL], api_failure=True)
    assert result.returncode == 1


def test_trunk_prs_stay_exempt_without_reading_labels(tmp_path: Path) -> None:
    result, calls = run_gate(tmp_path, live_labels=[], head_ref="trunk-merge/pr-42/x")
    assert result.returncode == 0, result.stderr
    assert calls == []
