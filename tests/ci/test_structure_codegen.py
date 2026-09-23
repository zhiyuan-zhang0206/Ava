"""Keep structure/codegen coverage and conservative CI selection in sync."""

import ast
import io
import os
import re
import shlex
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
HOOKS = {hook["id"]: hook for repo in CONFIG["repos"] for hook in repo["hooks"]}
CODEGEN = {hook_id for hook_id in HOOKS if hook_id.endswith("-fresh")}
JOB = WORKFLOW["jobs"]["backend-structure"]
STEPS = {step["name"]: step for step in JOB["steps"] if "name" in step}
LINT = STEPS["Run pre-commit structural gates (--all-files)"]
SELECT = STEPS["Select codegen freshness from changed files"]
FRESHNESS = STEPS["Run codegen freshness hooks (--all-files)"]
SCRIPT = SELECT["run"].split("<<'PY'\n", 1)[1].removesuffix("PY\n")
OTHER_CI = {
    "ruff",
    "ruff-format",
    "lint-migrations",
    "lint-clock-lattice",
    "frontend-vitest",
    "lint-core-content-manifests",
}
PREPUSH = {"pyright", "frontend-tsc", "frontend-eslint"}
LOCAL_ONLY = {"check-git-hooks-install"}


def select(event: str, paths: tuple[str, ...] = (), error: Exception | None = None):
    """Execute the actual workflow selector with a controlled Git response."""
    with TemporaryDirectory() as directory:
        output = Path(directory) / "output"
        env = {
            "EVENT_NAME": event,
            "LINT_STRUCTURE_BASELINE_BASE": "event-base-sha",
            "GITHUB_OUTPUT": str(output),
        }
        stdout = io.StringIO()
        changed = b"\0".join(os.fsencode(path) for path in paths)
        with (
            patch.dict(os.environ, env),
            patch("subprocess.check_output", return_value=changed, side_effect=error) as diff,
            redirect_stdout(stdout),
        ):
            exec(compile(SCRIPT, "ci.yml codegen selector", "exec"), {})
        return output.read_text(), stdout.getvalue(), diff.call_args_list


def test_required_job_identity_and_unconditional_lint() -> None:
    assert JOB["name"] == "backend structure (pre-commit lint + codegen freshness)"
    assert JOB["timeout-minutes"] == 25
    assert JOB["needs"] == ["classify"]
    assert JOB["if"] == (
        "${{ needs.classify.outputs.frontend == 'true' || "
        "needs.classify.outputs.backend == 'true' }}"
    )
    assert "if" not in LINT
    assert "continue-on-error" not in JOB
    assert "continue-on-error" not in LINT
    assert LINT["run"] == "env -u VIRTUAL_ENV uv run pre-commit run --all-files"


def test_codegen_invokes_exactly_the_four_configured_hooks() -> None:
    assert {
        "types-codegen-fresh",
        "constants-codegen-fresh",
        "events-registry-fresh",
        "config-lite-table-fresh",
    } == CODEGEN
    commands = [shlex.split(line) for line in FRESHNESS["run"].splitlines()]
    assert len(commands) == len(CODEGEN)
    assert all(
        command[:6] == ["env", "-u", "VIRTUAL_ENV", "uv", "run", "pre-commit"]
        for command in commands
    )
    assert all(command[6] == "run" and command[8:] == ["--all-files"] for command in commands)
    invoked = [command[7] for command in commands]
    assert set(invoked) == CODEGEN
    # A's SKIP must not leak into any of the explicit B invocations.
    assert "SKIP" not in JOB["env"]
    assert "SKIP" not in FRESHNESS.get("env", {})
    assert "continue-on-error" not in FRESHNESS


def test_four_hooks_exactly_partition_the_existing_structure_gate() -> None:
    skipped = set(LINT["env"]["SKIP"].split(","))
    assert skipped == OTHER_CI | PREPUSH | LOCAL_ONLY | CODEGEN
    commit_hooks = {
        hook_id
        for hook_id, hook in HOOKS.items()
        if "pre-commit" in hook.get("stages", CONFIG["default_stages"])
    }
    lint_hooks = commit_hooks - skipped
    assert not lint_hooks & CODEGEN
    assert lint_hooks | CODEGEN == commit_hooks - OTHER_CI - LOCAL_ONLY


def test_selector_ids_and_regexes_follow_hook_config() -> None:
    assignments = {
        node.targets[0].id: node.value
        for node in ast.parse(SCRIPT).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assert set(ast.literal_eval(assignments["codegen_ids"])) == CODEGEN
    assert ".pre-commit-config.yaml" in SCRIPT
    assert 'hooks[hook_id]["files"]' in SCRIPT
    for path in ("shared/events/", "ui/web/openapi.json", "shared/config/"):
        assert path in SCRIPT


def test_node_npm_and_codegen_share_one_condition() -> None:
    assert SELECT["id"] == "codegen"
    assert SELECT["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    for step in (
        STEPS["Install Node"],
        STEPS["Install frontend deps (codegen freshness)"],
        FRESHNESS,
    ):
        assert step["if"] == "steps.codegen.outputs.run == 'true'"
    order = list(STEPS)
    assert order.index("Fetch base revision for structure guard") < order.index(
        "Run pre-commit structural gates (--all-files)"
    )
    assert order.index("Run pre-commit structural gates (--all-files)") < order.index(
        "Select codegen freshness from changed files"
    )
    assert order.index("Select codegen freshness from changed files") < order.index("Install Node")


def test_every_codegen_input_family_selects_freshness() -> None:
    paths = (
        "gateway/schemas/agent.py",
        "gateway/app.py",
        "gateway/routers/agents.py",
        "shared/agents/contract.py",
        "shared/api_contracts/agent.py",
        "shared/tasks/priority.py",
        "shared/tasks/task_status.py",
        "shared/agents/history/timeline.py",
        "shared/agent_snapshot.py",
        "shared/resource_sample.py",
        "ops/rpc_schemas.py",
        "ops/rpc_terminate.py",
        "ops/cluster.py",
        "ui/web/src/lib/types-generated.ts",
        "ui/web/openapi.json",
        "shared/live_events.py",
        "scripts/dump_frontend_constants.py",
        "ui/web/src/lib/constants-generated.ts",
        "shared/events/contract.py",
        "shared/events/registry.py",
        "shared/events/registry_ops.py",
        "shared/events/system.py",
        "scripts/gen_event_registry.py",
        "shared/events/registry.md",
        "shared/config_registry.py",
        "shared/config/fields/core.py",
        "scripts/gen_config_lite_table.py",
        "shared/config_lite_table.json",
    )
    for path in paths:
        assert any(re.search(HOOKS[hook_id]["files"], path) for hook_id in CODEGEN), path
        output, log, calls = select("pull_request", (path,))
        assert output == "run=true\n", path
        assert "STEP SKIPPED" not in log
        assert calls[0].args[0] == [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            "event-base-sha",
            "HEAD",
        ]


def test_irrelevant_and_empty_diffs_skip_visibly() -> None:
    for paths in ((), ("db/schema.sql", "conventions/runbook.md", "ui/web/src/app/page.tsx")):
        output, log, _ = select("pull_request", paths)
        assert output == "run=false\n"
        assert log.splitlines() == [
            "STEP SKIPPED: codegen freshness — no codegen inputs changed in this PR; "
            "main pushes always run it; the merge queue re-runs the combined tree."
        ]


def test_push_and_manual_dispatch_always_run_without_diff() -> None:
    for event in ("push", "workflow_dispatch"):
        output, log, calls = select(event)
        assert output == "run=true\n" and "STEP SKIPPED" not in log
        assert not calls


def test_diff_and_shallow_history_failures_run_freshness() -> None:
    for error in (
        subprocess.CalledProcessError(128, "git diff", stderr=b"fatal: bad object"),
        FileNotFoundError("git"),
        KeyError("LINT_STRUCTURE_BASELINE_BASE"),
    ):
        output, log, _ = select("pull_request", error=error)
        assert output == "run=true\n"
        assert "::warning::Cannot compute codegen input diff" in log
        assert "STEP SKIPPED" not in log


def test_prepush_migration_keeps_direct_ci_owners() -> None:
    assert {
        hook_id for hook_id, hook in HOOKS.items() if hook.get("stages") == ["pre-push"]
    } == PREPUSH
    backend = WORKFLOW["jobs"]["backend-static"]["steps"]
    frontend = WORKFLOW["jobs"]["frontend"]["steps"]
    assert any(step.get("run") == "uv run pyright" for step in backend)
    assert any(step.get("run") == "npx tsc --noEmit" for step in frontend)
    assert any(step.get("run") == "npm run lint" for step in frontend)
