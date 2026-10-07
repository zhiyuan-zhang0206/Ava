"""What a push or a commit runs follows what the change touched.

The commit-stage ESLint run over changed files, the artifact hooks whose inputs a branch only
deleted, the gate that keeps a whole-repository check off a push that cannot move it, and the
nested branch-diff run that takes no load threshold. Each is exercised as a process against a
throwaway repository; `test_prepush_hooks.py` holds the guard, install and stage contracts.
"""

# ruff: noqa: S603 — subprocess commands use only test-owned paths and fixture literals.

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BRANCH_LINT = ROOT / "scripts/hooks/prepush-branch-lint.sh"
FRESHNESS = ROOT / "scripts/provision/prepush_freshness.py"
IF_CHANGED = ROOT / "scripts/hooks/prepush-if-changed.sh"
PRECOMMIT_ESLINT = ROOT / "scripts/hooks/precommit-eslint.sh"
SELECTOR = ROOT / "scripts/provision/prepush_frontend.py"


def _init_probe_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)


def _init_repo_with_origin_main(path: Path) -> str:
    """A repo with a local `origin/main` ref pointing at its first commit; returns that sha."""
    _init_probe_repo(path)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "base"], check=True)
    base_sha = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", base_sha], check=True
    )
    return base_sha


def _hooks() -> dict[str, dict[str, Any]]:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    return {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}


# ── the nested branch-diff run ──────────────────────────────────────────────────


def test_branch_lint_skips_without_precommit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_origin_main(repo)
    result = subprocess.run(
        ["bash", str(BRANCH_LINT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    assert "PRE-PUSH SKIPPED [branch-lint]: missing .venv/bin/pre-commit" in result.stderr


def test_branch_lint_runs_whatever_the_load_and_without_a_lock(tmp_path: Path) -> None:
    """The nested run costs about one commit over the same files, so a load threshold would only
    make rebased commits go unchecked at random: with the threshold at zero and no lock
    directory, it still runs the commit stage over merge-base..HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    base_sha = _init_repo_with_origin_main(repo)
    (repo / "change.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "change"], check=True)
    argv_log = tmp_path / "pre-commit-argv.json"
    fake = repo / ".venv/bin/pre-commit"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        f"#!{sys.executable}\nimport json, sys\njson.dump(sys.argv[1:], open({str(argv_log)!r}, 'w'))\n"
    )
    fake.chmod(0o755)
    result = subprocess.run(
        ["bash", str(BRANCH_LINT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**os.environ, "AVA_PREPUSH_MAX_LOAD_PER_CORE": "0"},
    )
    assert result.returncode == 0, result.stderr
    assert "SKIPPED" not in result.stderr
    assert json.loads(argv_log.read_text()) == [
        "run", "--hook-stage", "pre-commit", "--from-ref", base_sha, "--to-ref", "HEAD",
    ]  # fmt: skip


def test_eslint_keeps_commit_hook_and_scopes_push_contribution() -> None:
    """Keep the existing commit check and CI-compatible push hook ID."""
    hooks = _hooks()
    changed, whole = hooks["frontend-eslint"], hooks["frontend-eslint-full"]
    assert "stages" not in changed, "the changed-files run belongs to the commit stage"
    assert changed["entry"] == "bash scripts/hooks/precommit-eslint.sh"
    assert "pass_filenames" not in changed, "it must be handed the changed files"
    assert whole["stages"] == ["pre-push"]
    assert whole["pass_filenames"] is False
    assert whole["entry"] == ".venv/bin/python scripts/provision/prepush_frontend.py eslint"


# ── the commit-stage ESLint run over changed files ──────────────────────────────


@pytest.fixture
def eslint_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo with fake `npx` / `node` / `npm` that log how they were called, so the script's own
    decisions (which files, whole project or not) can be read back. Returns (repo, call log dir).

    Each tool logs to its own file: the script pipes `npx ... | node ...`, so the two run
    concurrently and one shared log would record them in whichever order they got scheduled."""
    repo = tmp_path / "repo"
    (repo / "ui/web/node_modules/.bin").mkdir(parents=True)
    (repo / "ui/web/node_modules/.bin/eslint").write_text("")
    (repo / "ui/web/node_modules/.bin/eslint").chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    calls = tmp_path / "calls"
    calls.mkdir()
    shims = tmp_path / "shims"
    shims.mkdir()
    for tool in ("npx", "node", "npm"):
        shim = shims / tool
        shim.write_text(f'#!/bin/sh\necho "{tool} $*" >> {calls / tool}\ncat > /dev/null\n')
        shim.chmod(0o755)
    return repo, calls


def _tool_calls(calls: Path, tool: str) -> list[str]:
    log = calls / tool
    return log.read_text().splitlines() if log.exists() else []


def _run_eslint_script(repo: Path, calls: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{calls.parent / 'shims'}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(PRECOMMIT_ESLINT), *paths],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env=env,
    )


def test_eslint_lints_just_the_changed_files(eslint_repo: tuple[Path, Path]) -> None:
    repo, calls = eslint_repo
    result = _run_eslint_script(repo, calls, "ui/web/src/a.tsx", "ui/web/src/lib/b.ts")
    assert result.returncode == 0, result.stderr
    assert _tool_calls(calls, "npx") == [
        "npx --no-install eslint --no-warn-ignored --format json src/a.tsx src/lib/b.ts"
    ]
    assert _tool_calls(calls, "node") == ["node scripts/check-eslint-warnings.mjs"]
    assert _tool_calls(calls, "npm") == []


@pytest.mark.parametrize(
    "setup_path",
    [
        "ui/web/eslint.config.mjs",
        "ui/web/eslint-rules/sentence-case.mjs",
        "ui/web/scripts/check-eslint-warnings.mjs",
        "ui/web/scripts/eslint-warning-baseline.json",
        "ui/web/package.json",
        "ui/web/package-lock.json",
        "ui/web/tsconfig.json",
    ],
)
def test_eslint_setup_change_lints_the_whole_project(
    eslint_repo: tuple[Path, Path], setup_path: str
) -> None:
    """The config, local rules, warning baseline and dependencies decide the verdict of files
    that did not change, so editing one of them (even next to a source file) is a full run."""
    repo, calls = eslint_repo
    result = _run_eslint_script(repo, calls, "ui/web/src/a.tsx", setup_path)
    assert result.returncode == 0, result.stderr
    assert _tool_calls(calls, "npm") == ["npm run lint"]
    assert _tool_calls(calls, "npx") == _tool_calls(calls, "node") == []


def test_eslint_hook_triggers_on_every_setup_file() -> None:
    """Every path the script widens on must reach it: the hook's `files:` selects them."""
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hook = {h["id"]: h for r in config["repos"] for h in r["hooks"]}["frontend-eslint"]
    selected = re.compile(hook["files"])
    for path in re.findall(r"ui/web/[\w./*-]+", PRECOMMIT_ESLINT.read_text().split("case", 1)[1]):
        assert selected.search(path.replace("*", "x.mjs")), path


def test_eslint_skips_loudly_without_node_modules(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = subprocess.run(
        ["bash", str(PRECOMMIT_ESLINT), "ui/web/src/a.tsx"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0
    assert "ESLINT SKIPPED: missing ui/web/node_modules" in result.stderr


# ── the artifact hooks whose inputs a branch only deleted ──────────────────────


@pytest.fixture
def freshness_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo carrying the real hook config and a copy of the freshness script, with a fake
    `.venv/bin/pre-commit` that logs the hook it was asked to run. Returns (repo, call log)."""
    repo = tmp_path / "repo"
    (repo / "scripts/provision").mkdir(parents=True)
    (repo / "scripts/hooks").mkdir(parents=True)
    (repo / "scripts/provision/prepush_freshness.py").write_text(FRESHNESS.read_text())
    (repo / "scripts/hooks/prepush-base.sh").write_text(
        (ROOT / "scripts/hooks/prepush-base.sh").read_text()
    )
    (repo / ".pre-commit-config.yaml").write_text((ROOT / ".pre-commit-config.yaml").read_text())
    calls = tmp_path / "calls.log"
    fake = repo / ".venv/bin/pre-commit"
    fake.parent.mkdir(parents=True)
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    fake.chmod(0o755)
    (repo / "ui/web/node_modules").mkdir(parents=True)  # types-codegen-fresh needs it
    return repo, calls


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)


def _run_freshness(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/provision/prepush_freshness.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _hooks_run(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return [line.split()[-1] for line in calls.read_text().splitlines()]


def _branch_from(repo: Path, files: dict[str, str]) -> None:
    """Commit `files` as the shared base, mark it origin/main, and leave HEAD on it."""
    _init_probe_repo(repo)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    (repo / ".gitignore").write_text(".venv/\nui/web/node_modules/\n")
    _commit_all(repo, "base")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", sha], check=True
    )


def test_a_branch_that_only_deletes_an_input_reruns_that_hook_over_the_repo(
    freshness_repo: tuple[Path, Path],
) -> None:
    repo, calls = freshness_repo
    _branch_from(repo, {"docs/gone.ava.okf.md": "x\n", "README.txt": "x\n"})
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "docs/gone.ava.okf.md"], check=True)
    _commit_all(repo, "delete only")
    result = _run_freshness(repo)
    assert result.returncode == 0, result.stderr
    # The OKF doc is gone and nothing added or changed a markdown path: lint-ava-okf and
    # check-doc-references both match the deleted path, and no other path of the branch.
    assert sorted(_hooks_run(calls)) == ["check-doc-references", "lint-ava-okf"]
    assert "--all-files" in calls.read_text()


def test_a_whole_repo_hook_is_left_to_the_branch_diff_run_when_the_branch_also_changes_an_input(
    freshness_repo: tuple[Path, Path],
) -> None:
    """The nested branch-diff run executes a whole-repository hook once the branch added or
    changed one of its inputs, and that run judges the deletion with it: no repeat."""
    repo, calls = freshness_repo
    _branch_from(repo, {"base/config/gone.py": "x = 1\n", "base/config/kept.py": "x = 1\n"})
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "base/config/gone.py"], check=True)
    (repo / "base/config/kept.py").write_text("x = 2\n")
    _commit_all(repo, "delete one config module, change another")
    assert _run_freshness(repo).returncode == 0
    assert _hooks_run(calls) == []


def test_a_per_file_hook_sees_the_deletion_only_through_the_sweep(
    freshness_repo: tuple[Path, Path],
) -> None:
    """lint-ava-okf judges only the documents it is handed, so a branch that deletes one OKF
    document and changes another still needs the whole-set re-check for links into the deleted
    one; the whole-repository markdown check is already covered by the nested run."""
    repo, calls = freshness_repo
    _branch_from(repo, {"docs/gone.ava.okf.md": "x\n", "docs/kept.ava.okf.md": "x\n"})
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "docs/gone.ava.okf.md"], check=True)
    (repo / "docs/kept.ava.okf.md").write_text("changed\n")
    _commit_all(repo, "delete one, change another")
    assert _run_freshness(repo).returncode == 0
    assert _hooks_run(calls) == ["lint-ava-okf"]


def test_moving_an_input_out_of_a_hook_pattern_counts_as_deleting_it(
    freshness_repo: tuple[Path, Path],
) -> None:
    repo, calls = freshness_repo
    _branch_from(repo, {"docs/a.ava.okf.md": "same text\n" * 5})
    subprocess.run(["git", "-C", str(repo), "mv", "docs/a.ava.okf.md", "docs/a.md"], check=True)
    _commit_all(repo, "rename out of the OKF pattern")
    assert _run_freshness(repo).returncode == 0
    # The old path leaves the OKF pattern; the new one is still markdown, which the branch-diff
    # run hands to check-doc-references, so only the OKF hook is left for the sweep.
    assert _hooks_run(calls) == ["lint-ava-okf"]


def test_a_branch_touching_no_artifact_input_runs_nothing(
    freshness_repo: tuple[Path, Path],
) -> None:
    repo, calls = freshness_repo
    _branch_from(repo, {"notes/a.txt": "x\n"})
    (repo / "notes/a.txt").write_text("changed\n")
    _commit_all(repo, "unrelated")
    result = _run_freshness(repo)
    assert result.returncode == 0, result.stderr
    assert _hooks_run(calls) == []
    assert "nothing to add" in result.stdout


def test_an_unknown_range_rejects_artifact_selection(freshness_repo: tuple[Path, Path]) -> None:
    repo, calls = freshness_repo
    _init_probe_repo(repo)
    _commit_all(repo, "base")
    result = _run_freshness(repo)
    assert result.returncode != 0
    assert _hooks_run(calls) == []
    assert "requires local origin/main" in result.stderr


def test_types_codegen_is_skipped_loudly_without_node_modules(
    freshness_repo: tuple[Path, Path],
) -> None:
    repo, calls = freshness_repo
    subprocess.run(["rm", "-rf", str(repo / "ui/web/node_modules")], check=True)
    _init_probe_repo(repo)
    _branch_from(repo, {"gateway/removed.py": "x = 1\n"})
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "gateway/removed.py"], check=True)
    _commit_all(repo, "delete codegen input")
    result = _run_freshness(repo)
    assert "PRE-PUSH SKIPPED [types-codegen-fresh]: missing ui/web/node_modules" in result.stderr
    assert "types-codegen-fresh" not in _hooks_run(calls)


def test_a_failing_artifact_hook_fails_the_push(freshness_repo: tuple[Path, Path]) -> None:
    repo, calls = freshness_repo
    (repo / ".venv/bin/pre-commit").write_text(f'#!/bin/sh\necho "$@" >> {calls}\nexit 1\n')
    _branch_from(repo, {"gateway/removed.py": "x = 1\n"})
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "gateway/removed.py"], check=True)
    _commit_all(repo, "delete codegen input")
    assert _run_freshness(repo).returncode == 1


# ── prepush-if-changed.sh: a whole-repo check that only runs when its inputs moved ──────────


def _run_if_changed(repo: Path, pattern: str, marker: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(IF_CHANGED), pattern, "--", "sh", "-c", f"echo RAN >> {marker}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_if_changed_skips_a_branch_with_no_matching_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_origin_main(repo)
    (repo / "notes.md").write_text("changed\n")
    _commit_all(repo, "docs only")
    marker = tmp_path / "ran"
    result = _run_if_changed(repo, r"\.py$", marker)
    assert result.returncode == 0
    assert not marker.exists()
    assert "skipping" in result.stdout


def test_if_changed_runs_for_an_added_changed_or_deleted_match(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_origin_main(repo)
    (repo / "keep.py").write_text("x = 1\n")
    _commit_all(repo, "add a py file")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", sha], check=True
    )
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "keep.py"], check=True)
    _commit_all(repo, "delete it")  # a deleted path must count: pre-commit never passes it
    marker = tmp_path / "ran"
    assert _run_if_changed(repo, r"\.py$", marker).returncode == 0
    assert marker.read_text() == "RAN\n"


def test_if_changed_rejects_unknown_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_probe_repo(repo)  # no commit, no origin/main
    marker = tmp_path / "ran"
    result = _run_if_changed(repo, r"\.py$", marker)
    assert result.returncode != 0
    assert "requires local origin/main" in result.stderr
    assert not marker.exists()


def test_if_changed_rejects_invalid_pattern(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_origin_main(repo)
    marker = tmp_path / "ran"
    result = _run_if_changed(repo, "[", marker)
    assert result.returncode != 0
    assert not marker.exists()
    assert "skipping" not in result.stdout


def test_patch_targets_full_scan_is_gated_on_python_changes() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hook = {h["id"]: h for r in config["repos"] for h in r["hooks"]}["lint-patch-targets-full"]
    assert hook["stages"] == ["pre-push"]
    assert hook["always_run"] is True  # the gate lives in the entry, which sees deletions
    assert hook["verbose"] is True
    assert shlex.split(hook["entry"]) == [
        "bash", "scripts/hooks/prepush-if-changed.sh", r"\.py$", "--",
        ".venv/bin/python", "scripts/lint/patch_targets.py",
    ]  # fmt: skip


# Frontend contribution scope across real rebase and pre-commit push ranges.


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fixture change")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(key, raising=False)
    result = tmp_path / "repo"
    result.mkdir()
    git(result, "init", "-q", "-b", "main")
    git(result, "config", "user.email", "test@example.com")
    git(result, "config", "user.name", "Test")
    commit(result, ".gitignore", "node_modules/\n")
    git(result, "update-ref", "refs/remotes/origin/main", "HEAD")
    bins = result / "ui/web/node_modules/.bin"
    bins.mkdir(parents=True)
    for name in ("vitest", "eslint", "next", "tsc"):
        (bins / name).symlink_to(sys.executable)
    probe = tmp_path / "probe"
    probe.mkdir()
    for name in ("node", "npm", "npx"):
        (probe / name).write_text(
            f"#!{sys.executable}\nimport os,json,sys\n"
            "if os.path.basename(sys.argv[0]) in {'npx', 'npm'}:\n"
            " with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
            " raise SystemExit(int(os.environ.get('TOOL_STATUS', '0')))\n"
            "sys.stdin.read()\n"
        )
        (probe / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(probe) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CALLS", str(result / "calls.jsonl"))
    monkeypatch.setenv("AVA_PREPUSH_MAX_LOAD_PER_CORE", "1000000")
    monkeypatch.setenv("AVA_PREPUSH_LOCK_DIR", str(tmp_path / "locks"))
    return result


def select(repo: Path, tool: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SELECTOR), tool],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def calls(repo: Path) -> list[list[str]]:
    log = repo / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize("tool", ["tsc", "eslint", "vitest"])
def test_rebased_upstream_ui_does_not_invoke_tool(repo: Path, tool: str) -> None:
    git(repo, "checkout", "-qb", "contribution")
    commit(repo, "notes.md", "own docs")
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    commit(repo, "ui/web/src/upstream.ts", "export const x = 1")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "contribution")
    git(repo, "rebase", "origin/main")
    assert "ui/web/src/upstream.ts" in git(repo, "diff", "--name-only", old, "HEAD")
    # Exercise actual pre-commit old-tip selection, not just the selector function.
    config = repo / ".pre-commit-config.yaml"
    legacy_command = {
        "tsc": "npx --no-install next typegen && npx --no-install tsc --noEmit",
        "eslint": "npm run lint",
        "vitest": "npx --no-install vitest run",
    }[tool]
    config.write_text(
        "repos:\n- repo: local\n  hooks:\n  - id: frontend\n    name: frontend\n"
        f"    entry: bash {ROOT / 'scripts/hooks/prepush-guard.sh'} {tool} -- bash -c 'cd ui/web && {legacy_command}'\n"
        "    language: system\n    stages: [pre-push]\n    files: ^ui/web/.*\\.(ts|tsx)$\n    pass_filenames: false\n"
    )
    legacy = subprocess.run(
        [
            str(ROOT / ".venv/bin/pre-commit"),
            "run",
            "--hook-stage",
            "pre-push",
            "--from-ref",
            old,
            "--to-ref",
            "HEAD",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert legacy.returncode == 0, legacy.stdout + legacy.stderr
    assert calls(repo), "the old push-tip trigger must reproduce the upstream-only tool invocation"
    (repo / "calls.jsonl").unlink()
    config.write_text(
        "repos:\n- repo: local\n  hooks:\n  - id: frontend\n    name: frontend\n"
        f"    entry: {sys.executable} {SELECTOR} {tool}\n"
        "    language: system\n    stages: [pre-push]\n    always_run: true\n    pass_filenames: false\n"
    )
    result = subprocess.run(
        [
            str(ROOT / ".venv/bin/pre-commit"),
            "run",
            "--hook-stage",
            "pre-push",
            "--from-ref",
            old,
            "--to-ref",
            "HEAD",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert calls(repo) == []


def test_new_branch_runs_own_source_and_disk_consumer(repo: Path) -> None:
    git(repo, "checkout", "-qb", "new-contribution")  # no remote branch exists
    commit(repo, "ui/web/src/widget.tsx", "export const x = 1")
    result = select(repo, "vitest")
    assert result.returncode == 0, result.stderr
    assert calls(repo) == [
        [
            "--no-install",
            "vitest",
            "run",
            "--passWithNoTests=false",
            "src/lib/localstorage-policy.test.ts",
        ],
        ["--no-install", "vitest", "related", "--run", "--passWithNoTests=false", "src/widget.tsx"],
    ]


@pytest.mark.parametrize("generated", ["ui/web/openapi.json", "ui/web/src/lib/types-generated.ts"])
def test_schema_type_artifacts_keep_contract_checks_without_related_run(
    repo: Path, generated: str
) -> None:
    assert re.search(_hooks()["types-codegen-fresh"]["files"], generated)
    commit(repo, generated, "{}" if generated.endswith(".json") else "export interface Example {}")
    result = select(repo, "tsc")
    assert result.returncode == 0, result.stderr
    assert calls(repo)[-1] == ["--no-install", "tsc", "--noEmit"]
    (repo / "calls.jsonl").unlink()
    result = select(repo, "vitest")
    assert result.returncode == 0, result.stderr
    assert all("related" not in command for command in calls(repo))
    if generated.endswith(".ts"):
        assert calls(repo)[0][-1] == "src/lib/localstorage-policy.test.ts"
    else:
        assert "require tsc and codegen freshness" in result.stdout


def test_type_artifact_does_not_hide_changed_runtime_source(repo: Path) -> None:
    commit(repo, "ui/web/src/lib/types-generated.ts", "export interface Example {}")
    commit(repo, "ui/web/scripts/runtime.mjs", "export const x = 1")
    result = select(repo, "vitest")
    assert result.returncode == 0, result.stderr
    assert calls(repo)[-1] == [
        "--no-install",
        "vitest",
        "related",
        "--run",
        "--passWithNoTests=false",
        "scripts/runtime.mjs",
    ]


def test_deleted_and_renamed_inputs_remain_visible(repo: Path) -> None:
    commit(repo, "ui/web/src/old name.ts", "export const x = 1")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "mv", "ui/web/src/old name.ts", "ui/web/src/new name.ts")
    git(repo, "commit", "-qm", "rename")
    result = select(repo, "vitest")
    assert result.returncode == 0, result.stderr
    assert "deleted-input closure requires CI" in result.stderr
    assert calls(repo)[-1][-1] == "src/new name.ts"
    (repo / "calls.jsonl").unlink()
    result = select(repo, "eslint")
    assert result.returncode == 0, result.stderr
    assert calls(repo)[0][-1] == "src/new name.ts"
    assert "src/old name.ts" not in calls(repo)[0]


def test_global_inputs_report_ci_gap_and_keep_known_disk_test(repo: Path) -> None:
    commit(repo, "ui/web/package.json", "{}")
    result = select(repo, "vitest")
    assert result.returncode == 0
    assert "UNVERIFIED" in result.stderr
    assert calls(repo) == [
        [
            "--no-install",
            "vitest",
            "run",
            "--passWithNoTests=false",
            "src/lib/frontend-bind.test.ts",
        ]
    ]


@pytest.mark.parametrize("tool", ["tsc", "eslint", "vitest"])
def test_missing_base_is_not_no_changes(repo: Path, tool: str) -> None:
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    result = select(repo, tool)
    assert result.returncode != 0
    assert "requires local origin/main" in result.stderr
    assert "no tool invoked" not in result.stdout
    assert calls(repo) == []


def test_empty_or_failed_related_run_stays_failed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit(repo, "ui/web/scripts/changed.mjs", "export const x = 1")
    monkeypatch.setenv("TOOL_STATUS", "1")
    result = select(repo, "vitest")
    assert result.returncode == 1
    assert calls(repo)[0][2:5] == ["related", "--run", "--passWithNoTests=false"]


@pytest.mark.parametrize(
    ("path", "consumer"),
    [
        ("services/entrypoints/gate/static/login.html", "src/lib/gate-login.test.ts"),
        ("tests/fixtures/events/chat_delta.json", "src/lib/event-fixtures.test.ts"),
        ("base/packages/plugins/ui_contributions.py", "src/components/plugin-nav-icon.test.ts"),
        ("ui/app/app-ui/locales/en.js", "src/app/app-ui-locale.test.ts"),
        ("ui/web/messages/en/core.json", "src/i18n/messages-layout.test.ts"),
        ("ui/web/src/app/globals.css", "src/app/globals-font-stack.test.ts"),
    ],
)
def test_known_filesystem_consumers_run_without_import_edge(
    repo: Path, path: str, consumer: str
) -> None:
    commit(repo, path, "fixture")
    result = select(repo, "vitest")
    assert result.returncode == 0, result.stderr
    assert consumer in calls(repo)[0]
    assert calls(repo)[0][2] == "run"


def test_delete_only_input_checks_type_project_and_reports_test_closure(repo: Path) -> None:
    commit(repo, "ui/web/src/removed.ts", "export const x = 1")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "rm", "ui/web/src/removed.ts")
    git(repo, "commit", "-qm", "delete")
    result = select(repo, "tsc")
    assert result.returncode == 0, result.stderr
    assert calls(repo) == [["--no-install", "next", "typegen"], ["--no-install", "tsc", "--noEmit"]]
    (repo / "calls.jsonl").unlink()
    result = select(repo, "vitest")
    assert "deleted-input closure requires CI" in result.stderr
    assert calls(repo) == [
        [
            "--no-install",
            "vitest",
            "run",
            "--passWithNoTests=false",
            "src/lib/localstorage-policy.test.ts",
        ]
    ]


def test_eslint_pipeline_preserves_native_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit(repo, "ui/web/src/changed.ts", "export const x = 1")
    monkeypatch.setenv("TOOL_STATUS", "7")
    result = select(repo, "eslint")
    assert result.returncode == 7
    assert calls(repo)[0][-1] == "src/changed.ts"
