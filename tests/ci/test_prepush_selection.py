"""What a push or a commit runs follows what the change touched.

The commit-stage ESLint run over changed files, the artifact hooks whose inputs a branch only
deleted, the gate that keeps a whole-repository check off a push that cannot move it, and the
nested branch-diff run that takes no load threshold. Each is exercised as a process against a
throwaway repository; `test_prepush_hooks.py` holds the guard, install and stage contracts.
"""

# ruff: noqa: S603 — subprocess commands use only test-owned paths and fixture literals.

import importlib
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
BRANCH_LINT = ROOT / "scripts/prepush-branch-lint.sh"
FRESHNESS = ROOT / "scripts/provision/prepush_freshness.py"
IF_CHANGED = ROOT / "scripts/prepush-if-changed.sh"
PRECOMMIT_ESLINT = ROOT / "scripts/precommit-eslint.sh"


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


def test_eslint_runs_over_changed_files_at_commit_and_over_the_project_at_push() -> None:
    """Twice by design: a hook is light only if its cost follows the change (commit), and a
    type-aware rule can react to a type that changed in another file, which a per-file run cannot
    see (push). The commit hook keeps the id CI's SKIP lists name."""
    hooks = _hooks()
    changed, whole = hooks["frontend-eslint"], hooks["frontend-eslint-full"]
    assert "stages" not in changed, "the changed-files run belongs to the commit stage"
    assert changed["entry"] == "bash scripts/precommit-eslint.sh"
    assert "pass_filenames" not in changed, "it must be handed the changed files"
    assert whole["stages"] == ["pre-push"]
    assert whole["pass_filenames"] is False
    assert "bash scripts/prepush-guard.sh eslint -- " in whole["entry"]


# ── the commit-stage ESLint run over changed files ──────────────────────────────


@pytest.fixture
def eslint_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo with fake `npx` / `node` / `npm` that log how they were called, so the script's own
    decisions (which files, whole project or not) can be read back. Returns (repo, call log)."""
    repo = tmp_path / "repo"
    (repo / "ui/web/node_modules/.bin").mkdir(parents=True)
    (repo / "ui/web/node_modules/.bin/eslint").write_text("")
    (repo / "ui/web/node_modules/.bin/eslint").chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    calls = tmp_path / "calls.log"
    shims = tmp_path / "shims"
    shims.mkdir()
    for tool in ("npx", "node", "npm"):
        shim = shims / tool
        shim.write_text(f'#!/bin/sh\necho "{tool} $*" >> {calls}\ncat > /dev/null\n')
        shim.chmod(0o755)
    return repo, calls


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
    logged = calls.read_text().splitlines()
    assert logged[0] == (
        "npx --no-install eslint --no-warn-ignored --format json src/a.tsx src/lib/b.ts"
    )
    assert logged[1] == "node scripts/check-eslint-warnings.mjs"
    assert not any(line.startswith("npm ") for line in logged)


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
    assert calls.read_text().splitlines() == ["npm run lint"]


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
    (repo / "scripts/provision/prepush_freshness.py").write_text(FRESHNESS.read_text())
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


def test_an_unknown_range_runs_every_artifact_hook(freshness_repo: tuple[Path, Path]) -> None:
    """No origin/main: not knowing what the branch changed is not a reason to skip."""
    repo, calls = freshness_repo
    _init_probe_repo(repo)
    _commit_all(repo, "base")
    result = _run_freshness(repo)
    assert result.returncode == 0, result.stderr
    assert sorted(_hooks_run(calls)) == sorted(
        importlib.import_module("scripts.provision.prepush_freshness").HOOKS
    )


def test_types_codegen_is_skipped_loudly_without_node_modules(
    freshness_repo: tuple[Path, Path],
) -> None:
    repo, calls = freshness_repo
    subprocess.run(["rm", "-rf", str(repo / "ui/web/node_modules")], check=True)
    _init_probe_repo(repo)
    _commit_all(repo, "base")  # no origin/main: every hook is due
    result = _run_freshness(repo)
    assert "PRE-PUSH SKIPPED [types-codegen-fresh]: missing ui/web/node_modules" in result.stderr
    assert "types-codegen-fresh" not in _hooks_run(calls)


def test_a_failing_artifact_hook_fails_the_push(freshness_repo: tuple[Path, Path]) -> None:
    repo, calls = freshness_repo
    (repo / ".venv/bin/pre-commit").write_text(f'#!/bin/sh\necho "$@" >> {calls}\nexit 1\n')
    _init_probe_repo(repo)
    _commit_all(repo, "base")
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


def test_if_changed_runs_when_the_range_is_unknown(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_probe_repo(repo)  # no commit, no origin/main
    marker = tmp_path / "ran"
    assert _run_if_changed(repo, r"\.py$", marker).returncode == 0
    assert marker.read_text() == "RAN\n"


def test_patch_targets_full_scan_is_gated_on_python_changes() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hook = {h["id"]: h for r in config["repos"] for h in r["hooks"]}["lint-patch-targets-full"]
    assert hook["stages"] == ["pre-push"]
    assert hook["always_run"] is True  # the gate lives in the entry, which sees deletions
    assert hook["verbose"] is True
    assert shlex.split(hook["entry"]) == [
        "bash", "scripts/prepush-if-changed.sh", r"\.py$", "--",
        ".venv/bin/python", "scripts/lint/patch_targets.py",
    ]  # fmt: skip
