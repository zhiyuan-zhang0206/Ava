"""Keep structure/codegen coverage and conservative CI selection in sync."""

import ast
import builtins
import io
import json
import os
import re
import shlex
import subprocess
from contextlib import contextmanager, nullcontext, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import yaml

from base.events.tests.codegen_sources import SourceGraph

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
    "lint-core-content-manifests",
}
# Hooks CI's structure gate skips because another job runs the same tool directly. The
# changed-files `frontend-eslint` is a commit-stage hook; its whole-project twin is pre-push.
CI_OWNED = {"pyright", "frontend-tsc", "frontend-eslint", "frontend-vitest"}
PREPUSH = {"pyright", "frontend-tsc", "frontend-eslint-full", "frontend-vitest"}
# Pre-push-stage-only hooks with no direct 1:1 CI job duplicate: local
# safety nets that re-run the pre-commit stage over the real branch diff, an
# unconditional rerun of the generated-artifact family, the full scan of the
# per-file patch-target lint (a production import change moves the home of tests
# it never passes) and the full tests-location check (a deleted test is never passed
# to a commit hook). Unlike PREPUSH, CI does not run these by name -- it already
# covers the same ground unconditionally via backend-structure's /
# merged-tree-structure's own `--all-files` runs of the underlying (filtered)
# hooks, so no CI job needs a matching direct invocation the way `uv run
# pyright` mirrors the pyright hook.
PREPUSH_LOCAL_ONLY = {
    "lint-prepush-branch-diff",
    "lint-prepush-artifact-freshness",
    "lint-patch-targets-full",
    "lint-tests-location-full",
}
LOCAL_ONLY = {"check-git-hooks-install"}
# These identities come from fastapi._compat.v2 / fastapi.openapi.utils, not
# repository class definitions. Keep exact names: new unknowns require review.
GENERATED_COMPONENTS = {
    "HTTPValidationError",
    "ValidationError",
    "Body_upload_files_api_agents__agent_id__uploads_post",
    "Body_upload_api_keyed_v1_agents__agent_id__uploads_post",
}


def assert_sources_covered(sources, pattern):
    missing = sorted(path for path in sources if not re.search(pattern, path))
    assert not missing, f"Uncovered codegen source modules: {missing}"


def test_openapi_component_sources_are_in_types_hook() -> None:
    components = json.loads((ROOT / "ui/web/openapi.json").read_text())["components"]["schemas"]
    assert components.keys() >= GENERATED_COMPONENTS
    sources = SourceGraph(ROOT).schema_sources(components, GENERATED_COMPONENTS)
    assert_sources_covered(sources, HOOKS["types-codegen-fresh"]["files"])


def test_event_contract_sources_are_in_events_hook() -> None:
    sources = SourceGraph(ROOT).imported_sources("base.events.contract")
    assert_sources_covered(sources, HOOKS["events-registry-fresh"]["files"])


def test_closure_follows_new_and_moved_definitions_without_name_collisions(tmp_path: Path) -> None:
    files = {
        "gateway/routers/probe.py": (
            "from gateway.schemas import WireModel\n"
            "@router.get('/probe')\ndef route() -> WireModel: ...\n"
        ),
        "gateway/schemas/__init__.py": "from gateway.schemas.probe import WireModel\n",
        "gateway/schemas/probe.py": "class WireModel: pass\n",
        "services/entrypoints/im_bridge/types.py": "class WireModel: pass\n",
        "base/new_model.py": "class NewModel: pass\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    assert SourceGraph(tmp_path).schema_sources({"WireModel"}, set()) == {
        "gateway/schemas/probe.py"
    }

    # A new annotation must be followed even before openapi.json is regenerated.
    (tmp_path / "gateway/schemas/probe.py").write_text(
        "from base.new_model import NewModel\nclass WireModel:\n    child: NewModel\n"
    )
    sources = SourceGraph(tmp_path).schema_sources({"WireModel"}, set())
    with TestCase().assertRaisesRegex(AssertionError, "base/new_model.py"):
        assert_sources_covered(sources, r"^gateway/")

    (tmp_path / "base/moved_model.py").write_text("class WireModel: pass\n")
    (tmp_path / "gateway/schemas/probe.py").write_text("from base.moved_model import WireModel\n")
    sources = SourceGraph(tmp_path).schema_sources({"WireModel"}, set())
    with TestCase().assertRaisesRegex(AssertionError, "base/moved_model.py"):
        assert_sources_covered(sources, r"^gateway/")


def test_closure_ignores_routes_declared_by_a_gateway_tests_directory(tmp_path: Path) -> None:
    files = {
        "gateway/routers/probe.py": (
            "from gateway.schemas import WireModel\n"
            "@router.get('/probe')\ndef route() -> WireModel: ...\n"
        ),
        "gateway/schemas/__init__.py": "from gateway.schemas.probe import WireModel\n",
        "gateway/schemas/probe.py": "class WireModel: pass\n",
        "gateway/tests/test_probe.py": (
            "class WireModel: pass\nclass FakeModel: pass\n"
            "@router.get('/fake')\ndef route() -> FakeModel: ...\n"
        ),
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    assert SourceGraph(tmp_path).schema_sources({"WireModel"}, set()) == {
        "gateway/schemas/probe.py"
    }


def test_events_closure_follows_payload_moves_outside_package(tmp_path: Path) -> None:
    sources = {
        "base/events/contract.py": "from .payloads import Spawn\n",
        "base/events/payloads.py": "from base.moved_payload import Spawn\n",
        "base/moved_payload.py": "class Spawn: pass\n",
    }
    for name, content in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    paths = SourceGraph(tmp_path).imported_sources("base.events.contract")
    assert paths == sources.keys()
    with TestCase().assertRaisesRegex(AssertionError, "base/moved_payload.py"):
        assert_sources_covered(paths, r"^base/events/")


def test_closure_resolves_module_alias_reexports_before_regeneration(tmp_path: Path) -> None:
    sources = {
        "gateway/routers/probe.py": (
            "from gateway.schemas.probe import WireModel\n"
            "@router.get('/probe')\ndef route() -> WireModel: ...\n"
        ),
        "gateway/schemas/probe.py": (
            "from base import public_models\nclass WireModel:\n    child: public_models.NewModel\n"
        ),
        "base/__init__.py": "from . import private_models as public_models\n",
        "base/private_models.py": "class NewModel: pass\n",
    }
    for name, content in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    paths = SourceGraph(tmp_path).schema_sources({"WireModel"}, set())
    with TestCase().assertRaisesRegex(AssertionError, "base/private_models.py"):
        assert_sources_covered(paths, r"^gateway/")


def test_closure_rejects_reachable_conditional_imports(tmp_path: Path) -> None:
    sources = {
        "gateway/routers/probe.py": (
            "from gateway.schemas.probe import WireModel\n"
            "@router.get('/probe')\ndef route() -> WireModel: ...\n"
        ),
        "gateway/schemas/probe.py": (
            "if flag:\n    from base.new_model import NewModel\nclass WireModel: pass\n"
        ),
        "base/new_model.py": "class NewModel: pass\n",
    }
    for name, content in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    # An unrelated TYPE_CHECKING/optional import must not reject the whole module.
    assert_sources_covered(SourceGraph(tmp_path).schema_sources({"WireModel"}, set()), r"^gateway/")
    schema = tmp_path / "gateway/schemas/probe.py"
    schema.write_text(
        schema.read_text().replace("class WireModel: pass", "class WireModel:\n    child: NewModel")
    )
    with TestCase().assertRaisesRegex(AssertionError, "Unsupported conditional schema binding"):
        SourceGraph(tmp_path).schema_sources({"WireModel"}, set())


def select(
    event: str, paths: tuple[str, ...] = (), error: Exception | None = None, selector_patch=None
):
    """Execute the actual workflow selector with a controlled Git response."""
    with TemporaryDirectory() as directory:
        output = Path(directory) / "output"
        env = {
            "EVENT_NAME": event,
            "LINT_STRUCTURE_BASELINE_BASE": "event-base-sha",
            "GITHUB_OUTPUT": str(output),
        }
        stdout = io.StringIO()
        failed = False
        changed = b"\0".join(os.fsencode(path) for path in paths)
        with (
            patch.dict(os.environ, env),
            patch("subprocess.check_output", return_value=changed, side_effect=error) as diff,
            redirect_stdout(stdout),
            selector_patch or nullcontext(),
        ):
            try:
                exec(compile(SCRIPT, "ci.yml codegen selector", "exec"), {})
            except SystemExit as exit_error:
                assert exit_error.code == 1
                failed = True
        # GitHub uses the last value for a repeated output key. No output also
        # selects B, so an unavailable output file cannot silently skip it.
        decision = output.read_text().splitlines()[-1] if output.exists() else "run=true"
        if failed:
            decision = "run=true"
        return decision + "\n", stdout.getvalue(), diff.call_args_list


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
    assert skipped == OTHER_CI | CI_OWNED | LOCAL_ONLY | CODEGEN
    commit_hooks = {
        hook_id
        for hook_id, hook in HOOKS.items()
        if "pre-commit" in hook.get("stages", CONFIG["default_stages"])
    }
    lint_hooks = commit_hooks - skipped
    assert not lint_hooks & CODEGEN
    assert lint_hooks | CODEGEN == commit_hooks - OTHER_CI - LOCAL_ONLY - CI_OWNED


def test_selector_ids_and_regexes_follow_hook_config() -> None:
    assignments = {
        node.targets[0].id: node.value
        for node in ast.walk(ast.parse(SCRIPT))
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assert set(ast.literal_eval(assignments["codegen_ids"])) == CODEGEN
    assert ".pre-commit-config.yaml" in SCRIPT
    assert 'hooks[hook_id]["files"]' in SCRIPT
    for path in ("base/events/", "ui/web/openapi.json", "base/config/"):
        assert path in SCRIPT


def test_node_npm_and_codegen_share_one_condition() -> None:
    assert SELECT["id"] == "codegen"
    assert SELECT["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert SELECT["continue-on-error"] is True
    for step in (
        STEPS["Install Node"],
        STEPS["Install frontend deps (codegen freshness)"],
        FRESHNESS,
    ):
        assert step["if"] == (
            "steps.codegen.outcome != 'success' || steps.codegen.outputs.run != 'false'"
        )
    order = list(STEPS)
    assert order.index("Fetch base revision for structure guard") < order.index(
        "Run pre-commit structural gates (--all-files)"
    )
    assert order.index("Run pre-commit structural gates (--all-files)") < order.index(
        "Select codegen freshness from changed files"
    )
    assert order.index("Select codegen freshness from changed files") < order.index("Install Node")


def test_structure_guard_pins_the_merge_refs_base_not_the_event_field() -> None:
    """#4903: base.sha can lag the merge ref's actual base (a stale branch's
    sync point), which hard-fails the guard once a baseline section retires.
    The depth-1 checkout carries no parent objects, so the step deepens the
    merge commit before resolving; resolution uses --verify because plain
    rev-parse echoes its own argument on failure (the literal string reached
    the fetch as an invalid refspec in run 37081265642). base.sha stays only
    as the fallback for a checkout that is not a merge ref."""
    script = STEPS["Fetch base revision for structure guard"]["run"]
    deepen = 'git fetch --depth=2 origin "$(git rev-parse HEAD)"'
    resolve = "git rev-parse --verify --quiet 'HEAD^1^{commit}'"
    fallback = 'base="${{ github.event.pull_request.base.sha }}"'
    base_fetch = 'git fetch --depth=1 origin "$base"'
    for needle in (deepen, resolve, fallback, base_fetch):
        assert needle in script
    assert (
        script.index(deepen)
        < script.index(resolve)
        < script.index(fallback)
        < script.index(base_fetch)
    )
    assert "git rev-parse --verify --quiet HEAD^2" in script
    assert "LINT_STRUCTURE_BASELINE_BASE" in script


def test_shallow_checkout_needs_the_deepen_before_resolving_head_parent(
    tmp_path: Path,
) -> None:
    """The mechanism the guard step relies on (run 37081265642): with the
    parent objects absent, plain rev-parse exits non-zero yet echoes the
    literal "HEAD^1" — --verify stays silent instead — and one depth-2
    re-fetch of the merge commit makes HEAD^1 the true base again."""
    source = tmp_path / "source"
    source.mkdir()

    def git(*args: str, cwd: Path = source) -> str:
        return subprocess.run(  # noqa: S603 — fixed git argv over the test's own fixture
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "structure-guard@example.invalid")
    git("config", "user.name", "structure-guard")
    # The fixture server models GitHub, which serves fetch-by-SHA requests.
    git("config", "uploadpack.allowAnySHA1InWant", "true")
    (source / "a").write_text("a\n")
    git("add", "a")
    git("commit", "-qm", "base one")
    git("checkout", "-qb", "feature")
    (source / "b").write_text("b\n")
    git("add", "b")
    git("commit", "-qm", "feature head")
    git("checkout", "-q", "main")
    (source / "m").write_text("m\n")
    git("add", "m")
    git("commit", "-qm", "base two")
    git("merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    base = git("rev-parse", "HEAD^1")

    checkout = tmp_path / "checkout"
    subprocess.run(  # noqa: S603 — fixed git argv over the test's own fixture
        ["git", "clone", "-q", "--depth=1", source.as_uri(), str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    literal = subprocess.run(
        ["git", "rev-parse", "HEAD^1"],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
    )
    assert literal.returncode != 0
    assert literal.stdout.strip() == "HEAD^1"
    silent = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD^1^{commit}"],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
    )
    assert silent.returncode != 0
    assert silent.stdout.strip() == ""
    merge = git("rev-parse", "HEAD", cwd=checkout)
    subprocess.run(  # noqa: S603 — fixed git argv over the test's own fixture
        ["git", "fetch", "-q", "--depth=2", "origin", merge],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    assert git("rev-parse", "HEAD^1", cwd=checkout) == base


def test_every_codegen_input_family_selects_freshness() -> None:
    paths = (
        "gateway/schemas/tasks.py",
        "gateway/agents/schemas.py",
        "gateway/app.py",
        "gateway/routers/tasks.py",
        "gateway/agents/router.py",
        "base/agents/birth_config.py",
        "base/agents/contract.py",
        "base/api_contracts/contracts.py",
        "base/agents/tasks/priority.py",
        "base/agents/tasks/status.py",
        "base/agents/history/timeline.py",
        "base/agents/observation/roster.py",
        "base/agents/observation/evidence.py",
        "base/agents/history/timeline_item.py",
        "ops/rpc_schemas/messages.py",
        "ops/rpc_schemas/billing_recovery.py",
        "ops/cluster_status/__init__.py",
        "base/agents/impersonation/history.py",
        "base/agents/sdk/telemetry.py",
        "base/agents/observation/snapshot.py",
        "base/host/resource_sample.py",
        "ops/rpc_schemas/__init__.py",
        "ops/rpc_schemas/terminate.py",
        "ui/web/src/lib/contracts/types-generated.ts",
        "ui/web/openapi.json",
        "base/events/live/projection.py",
        "scripts/codegen/dump_frontend_constants.py",
        "ui/web/src/lib/contracts/constants-generated.ts",
        "base/events/contract.py",
        "base/events/vocabulary.py",
        "base/events/loader.py",
        "base/events/declarations/__init__.py",
        "base/events/declarations/host.py",
        "scripts/codegen/gen_event_registry.py",
        "base/events/registry.md",
        "base/host/env/config_registry.py",
        "base/config/domains/agent/settings.py",
        "scripts/codegen/gen_config_lite_table.py",
        "base/host/env/config_lite_table.json",
    )
    for path in paths:
        assert (ROOT / path).is_file(), f"Freshness matrix path no longer exists: {path}"
        assert any(re.search(HOOKS[hook_id]["files"], path) for hook_id in CODEGEN), path
        # Reuse the real parsed config across the path matrix; parser failures
        # are covered separately without repeating YAML parsing for every path.
        output, log, calls = select(
            "pull_request", (path,), selector_patch=patch("yaml.safe_load", return_value=CONFIG)
        )
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
    for paths in ((), ("db/schema.sql", "docs/conventions/runbook.md", "ui/web/src/app/page.tsx")):
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
        assert "::warning::Codegen selector failed" in log
        assert "STEP SKIPPED" not in log


def test_selector_config_and_runtime_failures_run_freshness() -> None:
    for target, error in (
        ("pathlib.Path.read_text", PermissionError("config unreadable")),
        ("yaml.safe_load", yaml.YAMLError("invalid YAML")),
        ("re.compile", re.error("invalid regex")),
        ("re.compile", RuntimeError("unexpected selector error")),
        ("pathlib.Path.open", OSError("output unavailable")),
    ):
        output, log, _ = select(
            "pull_request",
            ("base/agents/observation/roster.py",),
            selector_patch=patch(target, side_effect=error),
        )
        assert output == "run=true\n"
        assert "::warning::Codegen selector failed" in log
        assert "STEP SKIPPED" not in log


def test_missing_hook_defaults_to_run() -> None:
    with patch("yaml.safe_load", return_value={"repos": []}):
        output, log, _ = select("pull_request", ("base/agents/observation/roster.py",))
    assert output == "run=true\n"
    assert "::warning::Codegen selector failed (KeyError)" in log


def test_failure_after_skip_output_is_written_still_runs_freshness() -> None:
    original = Path.open
    written = []
    appends = 0

    @contextmanager
    def opening(path, *args, **kwargs):
        nonlocal appends
        with original(path, *args, **kwargs) as stream:
            yield stream
        if args == ("a",):
            appends += 1
            if appends == 2:
                with original(path) as output:
                    written.append(output.read())
                raise OSError("close failed after publishing skip")

    output, log, _ = select("pull_request", selector_patch=patch.object(Path, "open", opening))
    assert written == ["run=true\nrun=false\n"]
    assert output == "run=true\n"
    assert "::warning::Codegen selector failed (OSError)" in log
    assert "STEP SKIPPED" not in log


def test_selector_import_failure_defaults_to_run() -> None:
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("yaml unavailable")
        return original(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=importing):
        output, log, _ = select("pull_request", ("base/agents/observation/roster.py",))
    assert output == "run=true\n"
    assert "::warning::Codegen selector failed (ImportError)" in log


def test_prepush_migration_keeps_direct_ci_owners() -> None:
    assert {
        hook_id for hook_id, hook in HOOKS.items() if hook.get("stages") == ["pre-push"]
    } == PREPUSH | PREPUSH_LOCAL_ONLY
    for hook_id in PREPUSH_LOCAL_ONLY:
        assert HOOKS[hook_id]["always_run"] is True
    backend = WORKFLOW["jobs"]["backend-static"]["steps"]
    frontend = WORKFLOW["jobs"]["frontend"]["steps"]
    assert any(step.get("run") == "uv run pyright" for step in backend)
    assert any(step.get("run") == "npx tsc --noEmit" for step in frontend)
    assert any(step.get("run") == "npm run lint" for step in frontend)
    assert any(
        step.get("run") == "npx vitest run --config tests/vitest.config.mts --coverage"
        for step in frontend
    )
