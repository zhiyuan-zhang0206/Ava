"""Execute the real retry shell with a mock GitHub API; never call the network.

The checked-in ci-rerun script needs bash >= 4 (`${VAR,,}`); macOS ships bash
3.2 as /bin/bash, so the runner resolves a modern bash explicitly and this
file skips with a clear reason when only an older one exists on macOS (see
`_resolve_bash`)."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = "owner/repository"
SHA = "a" * 40
OTHER_SHA = "b" * 40

FAMILY_JOB = "cold-offline (ubuntu-24.04)"
FAMILY_STEP = "Real offline prepare, retained interpreter and failure isolation"

KNOWN_FAMILY_JOBS = {
    "jobs": [
        {
            "name": FAMILY_JOB,
            "conclusion": "failure",
            "steps": [
                {"name": "Checkout", "conclusion": "success"},
                {"name": FAMILY_STEP, "conclusion": "failure"},
                {"name": "Upload artifacts", "conclusion": "skipped"},
            ],
        },
        {
            "name": "cold-offline (macos-14)",
            "conclusion": "success",
            "steps": [{"name": FAMILY_STEP, "conclusion": "success"}],
        },
    ]
}

DOUBLE_FAULT_JOBS = {
    "jobs": [
        {
            "name": FAMILY_JOB,
            "conclusion": "failure",
            "steps": [{"name": FAMILY_STEP, "conclusion": "failure"}],
        },
        {
            "name": "cold-offline (macos-14)",
            "conclusion": "failure",
            "steps": [{"name": FAMILY_STEP, "conclusion": "failure"}],
        },
    ]
}


def _resolve_bash(candidates: tuple[str | None, ...] | None = None) -> str | None:
    """Resolve a bash >= 4 for the checked-in retry script, or None.

    The script uses bash-4 lowercase expansion (`${VAR,,}`), which bash 3.2
    cannot parse; candidates are probed for their BASH_VERSINFO major — the
    expansion itself cannot be probed (it is the feature under test). The
    default search: PATH's bash, then the common Homebrew/local prefixes, then
    /bin/bash. Tests pass fake candidates."""
    if candidates is None:
        candidates = (
            shutil.which("bash"),
            "/opt/homebrew/bin/bash",
            "/usr/local/bin/bash",
            "/bin/bash",
        )
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None or candidate in seen or not os.access(candidate, os.X_OK):
            continue
        seen.add(candidate)
        probe = subprocess.run(  # noqa: S603 — resolved local bash, not untrusted input
            [candidate, "-c", 'echo "${BASH_VERSINFO[0]}"'],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        major = probe.stdout.strip()
        if probe.returncode == 0 and major.isdigit() and int(major) >= 4:
            return candidate
    return None


BASH = _resolve_bash()

# Skip only on macOS: an unusable bash here is the expected 3.2-as-/bin/bash
# case (a clear reason beats 23 phantom failures), while on any other platform
# a missing modern bash is an environment defect that must fail loudly instead
# of silently dropping this file's coverage.
pytestmark = pytest.mark.skipif(
    BASH is None and sys.platform == "darwin",
    reason="the ci-rerun script needs bash >= 4 (`${VAR,,}`) and macOS ships 3.2 "
    "as /bin/bash — install a modern bash to run this file locally",
)


def retry_script() -> str:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci-rerun.yml").read_text())
    return workflow["jobs"]["rerun-failed-jobs"]["steps"][0]["run"]


def run_retry(
    tmp_path: Path, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    script = retry_script()
    mock = tmp_path / "gh"
    mock.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['CALL_LOG'], 'a') as log: log.write(json.dumps(args) + '\\n')\n"
        "if os.environ.get('API_FAIL') == '1': sys.exit(1)\n"
        "if '--method' in args: sys.exit(0)\n"
        "endpoint = args[1]\n"
        "if '/pulls/' in endpoint: print(os.environ['PR_RESPONSE'])\n"
        "elif '/commits/' in endpoint: print(os.environ['CURRENT_SHA'])\n"
        "elif '/jobs' in endpoint:\n"
        "    # Run the real --jq program against the payload, the way gh does.\n"
        "    done = subprocess.run(\n"
        "        ['jq', '-r', args[args.index('--jq') + 1]],\n"
        "        input=os.environ['JOBS_PAYLOAD'], capture_output=True, text=True,\n"
        "    )\n"
        "    sys.stdout.write(done.stdout)\n"
        "    sys.stderr.write(done.stderr)\n"
        "    sys.exit(done.returncode)\n"
        "else: print(os.environ['NEWEST_RUN'])\n"
    )
    mock.chmod(0o700)
    call_log = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.defpath}",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "RUN_ID": "100",
        "RUN_ATTEMPT": "1",
        "RUN_CONCLUSION": "failure",
        "RUN_EVENT": "pull_request",
        "WORKFLOW_NAME": "CI",
        "HEAD_SHA": SHA,
        "HEAD_BRANCH": "feature",
        "HEAD_REPO": REPO,
        "PR_NUMBER": "42",
        "PR_RESPONSE": f"open\t{SHA}\t{REPO}\t{REPO}",
        "CURRENT_SHA": SHA,
        "NEWEST_RUN": "100",
        "JOBS_PAYLOAD": json.dumps(KNOWN_FAMILY_JOBS),
        "CALL_LOG": str(call_log),
        "API_FAIL": "0",
        **overrides,
    }
    assert BASH is not None, "no bash >= 4 resolved for the ci-rerun script"
    result = subprocess.run(  # noqa: S603 — checked-in shell; gh is an isolated mock
        [BASH, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    calls = (
        [json.loads(line) for line in call_log.read_text().splitlines()]
        if call_log.exists()
        else []
    )
    return result, calls


def test_resolve_bash_requires_version_four_and_takes_the_first_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Selection logic of the skip's resolver: older candidates are rejected,
    the first >= 4 wins, and an all-old set resolves to None (the skip case)."""
    old = tmp_path / "bash-old"
    old.write_text("")
    old.chmod(0o700)
    new = tmp_path / "bash-new"
    new.write_text("")
    new.chmod(0o700)

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        major = "3" if argv[0] == str(old) else "5"
        return subprocess.CompletedProcess(argv, 0, stdout=f"{major}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _resolve_bash((str(old), str(new))) == str(new)
    assert _resolve_bash((str(old),)) is None


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_current_head_retries_once(tmp_path: Path, event: str) -> None:
    result, calls = run_retry(tmp_path, RUN_EVENT=event)
    assert result.returncode == 0, result.stderr
    posts = [call for call in calls if "POST" in call]
    assert posts == [
        ["api", "--method", "POST", f"repos/{REPO}/actions/runs/100/rerun-failed-jobs"]
    ]


def test_cap_cancelled_run_reruns_once(tmp_path: Path) -> None:
    """A cap-cancelled run (`conclusion=cancelled` — a job hitting its own
    timeout-minutes cancels the run) is re-run once, like a failure
    (2026-09-12 cap cancels; task #3239)."""
    result, calls = run_retry(tmp_path, RUN_CONCLUSION="cancelled")
    assert result.returncode == 0, result.stderr
    posts = [call for call in calls if "POST" in call]
    assert posts == [
        ["api", "--method", "POST", f"repos/{REPO}/actions/runs/100/rerun-failed-jobs"]
    ]


def test_verified_fork_head_is_not_confused_with_base_repository(tmp_path: Path) -> None:
    fork = "contributor/repository"
    result, calls = run_retry(tmp_path, HEAD_REPO=fork, PR_RESPONSE=f"open\t{SHA}\t{REPO}\t{fork}")
    assert result.returncode == 0, result.stderr
    assert sum("POST" in call for call in calls) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"PR_RESPONSE": f"open\t{OTHER_SHA}\t{REPO}\t{REPO}"},
        {"PR_RESPONSE": f"closed\t{SHA}\t{REPO}\t{REPO}"},
        {"PR_RESPONSE": f"open\t{SHA}\tother/repo\t{REPO}"},
        {"PR_RESPONSE": f"open\t{SHA}\t{REPO}\tother/repo"},
        {"PR_NUMBER": ""},
        {"PR_NUMBER": "42/other"},
        {"RUN_ATTEMPT": "2"},
        {"RUN_CONCLUSION": "skipped"},
        {"NEWEST_RUN": "101"},
        {"RUN_EVENT": "push", "CURRENT_SHA": OTHER_SHA},
        {"RUN_EVENT": "push", "HEAD_REPO": "other/repo"},
        {"RUN_EVENT": "workflow_dispatch"},
    ],
)
def test_stale_or_unverified_event_never_posts(tmp_path: Path, overrides: dict[str, str]) -> None:
    result, calls = run_retry(tmp_path, **overrides)
    assert result.returncode == 0, result.stderr
    assert not any("POST" in call for call in calls)


def test_api_failure_is_not_permission_to_retry(tmp_path: Path) -> None:
    result, calls = run_retry(tmp_path, API_FAIL="1")
    assert result.returncode != 0
    assert not any("POST" in call for call in calls)


def test_cross_sha_guard_race_cannot_share_native_concurrency_group() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    group = workflow["concurrency"]["group"]
    expression = "${{ github.sha }}"
    assert group == "ci-${{ github.ref }}-" + expression
    ref = "refs/pull/42/merge"
    old = group.replace("${{ github.ref }}", ref).replace(expression, SHA)
    new = group.replace("${{ github.ref }}", ref).replace(expression, OTHER_SHA)
    # Native Actions cancellation/replacement only applies inside one group.
    # This remains different if PR synchronization happens after the GET guard.
    assert old != new
    assert old == group.replace("${{ github.ref }}", ref).replace(expression, SHA)


def test_runtime_prepare_known_family_reruns_once(tmp_path: Path) -> None:
    """The second whitelisted workflow retries only after the jobs probe
    confirms the known cold-offline family (task #3285)."""
    result, calls = run_retry(
        tmp_path,
        WORKFLOW_NAME="Inactive runtime preparation",
        JOBS_PAYLOAD=json.dumps(KNOWN_FAMILY_JOBS),
    )
    assert result.returncode == 0, result.stderr
    posts = [call for call in calls if "POST" in call]
    assert posts == [
        ["api", "--method", "POST", f"repos/{REPO}/actions/runs/100/rerun-failed-jobs"]
    ]
    probes = [call for call in calls if any("/jobs" in arg for arg in call)]
    assert len(probes) == 1
    assert "--jq" in probes[0]


def test_ci_flow_never_queries_job_conclusions(tmp_path: Path) -> None:
    result, calls = run_retry(tmp_path)
    assert result.returncode == 0, result.stderr
    assert any("POST" in call for call in calls)
    assert not any("/jobs" in arg for call in calls for arg in call)


@pytest.mark.parametrize(
    "overrides",
    [
        {"JOBS_PAYLOAD": json.dumps(DOUBLE_FAULT_JOBS)},
        {"RUN_ATTEMPT": "2"},
        {"RUN_CONCLUSION": "skipped"},
        {"NEWEST_RUN": "101"},
        {"PR_RESPONSE": f"closed\t{SHA}\t{REPO}\t{REPO}"},
    ],
)
def test_runtime_prepare_unconfirmed_never_posts(tmp_path: Path, overrides: dict[str, str]) -> None:
    result, calls = run_retry(tmp_path, WORKFLOW_NAME="Inactive runtime preparation", **overrides)
    assert result.returncode == 0, result.stderr
    assert not any("POST" in call for call in calls)


def test_trigger_whitelist_names_both_workflows() -> None:
    # YAML 1.1 parses the bare `on` key as boolean True, not the string "on".
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci-rerun.yml").read_text())
    assert workflow[True]["workflow_run"]["workflows"] == [
        "CI",
        "Inactive runtime preparation",
    ]
    env = workflow["jobs"]["rerun-failed-jobs"]["env"]
    assert env["WORKFLOW_NAME"] == "${{ github.event.workflow_run.name }}"


def test_runtime_prepare_family_names_stay_coupled() -> None:
    """The family gate hard-codes the job and step names below; renaming
    either side of the coupling must fail here, not silently refuse retries."""
    prepare = yaml.safe_load((ROOT / ".github/workflows/runtime-prepare.yml").read_text())
    job = prepare["jobs"]["cold-offline"]
    ubuntu = [entry for entry in job["strategy"]["matrix"]["os"] if entry.startswith("ubuntu")]
    assert ubuntu == ["ubuntu-24.04"]
    steps = [step["name"] for step in job["steps"] if "name" in step]
    assert FAMILY_STEP in steps

    script = retry_script()
    assert f"cold-offline ({ubuntu[0]})" in script
    assert FAMILY_STEP in script


def test_family_expression_matches_only_the_known_shape() -> None:
    """Mock gh feeds the jobs probe a pre-decided verdict; this evaluates the
    real jq expression against job shapes the GitHub API returns."""
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is required to evaluate the family expression")
    found = re.search(r"--jq '(\[\.jobs[^']*)'", retry_script())
    assert found, "family jq expression not found in the retry script"

    def job(name: str, conclusion: str | None, steps: dict[str, str | None]) -> dict[str, object]:
        return {
            "name": name,
            "conclusion": conclusion,
            "steps": [
                {"name": step_name, "conclusion": step_conclusion}
                for step_name, step_conclusion in steps.items()
            ],
        }

    healthy = job("cold-offline (macos-14)", "success", {FAMILY_STEP: "success"})
    known = job(
        FAMILY_JOB,
        "failure",
        {"Checkout": "success", FAMILY_STEP: "failure", "Upload artifacts": "skipped"},
    )
    cases = [
        ("known shape", "match", [known, healthy]),
        (
            "watchdog step reports timed_out",
            "match",
            [
                job(FAMILY_JOB, "failure", {"Checkout": "success", FAMILY_STEP: "timed_out"}),
                healthy,
            ],
        ),
        (
            "second failing step",
            "mismatch",
            [
                job(
                    FAMILY_JOB,
                    "failure",
                    {"Checkout": "success", FAMILY_STEP: "failure", "Upload": "failure"},
                ),
                healthy,
            ],
        ),
        (
            "different failing step",
            "mismatch",
            [job(FAMILY_JOB, "failure", {"Checkout": "success", "Type-check": "failure"}), healthy],
        ),
        (
            "second failing job",
            "mismatch",
            [known, job("cold-offline (macos-14)", "failure", {FAMILY_STEP: "failure"})],
        ),
        (
            "cancelled step",
            "mismatch",
            [
                job(FAMILY_JOB, "failure", {"Checkout": "success", FAMILY_STEP: "cancelled"}),
                healthy,
            ],
        ),
        (
            "job-level cancelled",
            "mismatch",
            [job(FAMILY_JOB, "cancelled", {FAMILY_STEP: "failure"}), healthy],
        ),
        (
            "missing steps list",
            "mismatch",
            [{"name": FAMILY_JOB, "conclusion": "failure"}, healthy],
        ),
        (
            "fully green run",
            "mismatch",
            [healthy, job(FAMILY_JOB, "success", {FAMILY_STEP: "success"})],
        ),
        (
            "still-running job",
            "mismatch",
            [job(FAMILY_JOB, None, {FAMILY_STEP: "success"}), healthy],
        ),
    ]
    for label, expected, jobs in cases:
        payload = {"jobs": jobs}
        result = subprocess.run(  # noqa: S603 — local jq, checked-in expression
            [jq, "-r", found.group(1)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert result.stdout.strip() == expected, f"{label}: {payload}"


def test_dispatcher_prefilter_mirrors_the_first_guard_clauses() -> None:
    """The first guard clauses now gate the job itself: a run whose conclusion /
    attempt / event already disqualifies it — or a merge-queue car — never
    schedules a runner (evaluation #4627 R1). The step's guard chain stays as
    defense in depth; the behavioral tests above pin its outcomes."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci-rerun.yml").read_text())
    condition = " ".join(str(workflow["jobs"]["rerun-failed-jobs"]["if"]).split())
    expected = """\
        contains(fromJSON('["failure","timed_out","action_required","startup_failure","cancelled"]'),
                  github.event.workflow_run.conclusion)
        && github.event.workflow_run.run_attempt == 1
        && (github.event.workflow_run.event == 'pull_request'
            || github.event.workflow_run.event == 'push')
        && !startsWith(github.event.workflow_run.head_branch, 'trunk-merge/')"""
    assert condition == " ".join(expected.split())
    assert "does not need a rerun" in retry_script()
