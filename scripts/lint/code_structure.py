"""Structural lints that keep the codebase legible to agents: no `TYPE_CHECKING`
import-folding, role-call allowlisting, and frozen structure/function budgets.

Run: `.venv/bin/python scripts/lint/code_structure.py [path ...]` (defaults to the
whole repo; an explicit path that does not exist is an error (stderr + exit 1)
rather than a silent no-op). Also run automatically via pre-commit hook before
commit.

## Why

A fully agent-generated codebase is optimized first for the agent's ability to
reason over it directly. These structural rules protect that:

### Rule 1: no `if TYPE_CHECKING:` import folding

All dependencies are imported at top level. Reasons (see AGENTS.md "Python
coding conventions"): an application's deps are always on the runtime path, so
the startup-cost TYPE_CHECKING saves does not exist (the module is already
cached); and LangGraph / Pydantic introspect type hints at runtime via
`get_type_hints()` / `inspect.signature`, so a name imported only under
`TYPE_CHECKING` raises NameError. The genuine exceptions — a real circular
import, or an `import torch`-class heavy dependency on a path that does not use
the type — go in `_TYPE_CHECKING_ALLOWED` with a one-line reason.

### Rule 3: `machine_role()` allowlist

`machine_role()` answers "what does this host serve" and must never be used to
decide *where an operation runs* — the gateway is the single routing point, and
every CLI routes to it (user ruling 2026-08-21, issue #216). The line is not
*where* the function is called but *what the answer is used for*:

- **legitimate** — "what do I serve?": start the right daemons, advertise
  honestly in register_self, audit this host, guard a capability.
- **illegitimate** — "should I do this myself, or ask the gateway?": a module
  that implements an operation must not branch on role.

So instead of banning the call (legitimate uses exist) we allowlist it:
`_MACHINE_ROLE_ALLOWED` enumerates the modules that may call `machine_role()`,
each with a one-line reason naming the question it answers. A call anywhere
else fails the run; an allowlisted module that stops calling it also fails
(stale-entry alert, the `unmatched_ignore_imports_alerting` shape from #176)
so the list cannot rot into a permission wall.

### Rule 4: package doors (locality)

A `_`-prefixed module or name is private to the package that owns it, resolved
the way Python resolves it: when `<prefix>.py` exists the name is package-private
to that module's package (a same-named docs folder beside it changes nothing);
otherwise the prefix is the package owning the private submodule. Reaching it from
outside that package, by import or by attribute access on an imported module
(`import ava.x as m; m._y`), bypasses the package door: the importer depends on an
implementation detail the owner never promised to keep. Fix: use a public name
through the owner's `__init__.py`, or promote the name into the owner's contract
on purpose (export it / drop the underscore) so the widened contract is visible in
the diff. No per-site allowlist: a name another package needs is contract by
definition. `ava` is no exception: agent visibility there is the
`__all_for_ava__` whitelist, not the underscore. A module alias rebound anywhere
in the file (a parameter such as `self`, a local) is not followed.

Test files (any `tests/` directory: the top-level one or a package's own, see
`lint_common.is_test_path`) are exempt from Rules 1, 3, 4, 5 and 6; only budgets apply
(patch targets in tests: `scripts/lint/patch_targets.py`).

### Rule 5: single decision owners (locality)

`scripts/structure/locality.py:DECISIONS` names design decisions that have exactly
one owning module; any other module making that decision is a bypass. Today:
`postgres-dial` — a psycopg connect (module, class, or `from psycopg import
connect`) or a construction of a psycopg_pool pool or of this repo's own
`*ConnectionPool` subclass belongs to `base/db/connections.py`, which owns the
transport posture. A site that genuinely cannot go through the owner goes in that
decision's `allowed` map with a one-line reason; an allowed module that stops
bypassing, or no longer exists, fails as stale.

Rules 4 and 5 have no baseline allowances. Every measured reach-in or decision-owner
bypass fails directly. The owner resolution and decision contracts remain in
scripts/structure/locality.py; see docs/conventions/python-conventions.md.

### Rule 6: no path imports under ava_builtins/ (package doors)

`scripts/structure/path_imports.py`: a skill or plugin module may not edit `sys.path`, call
`site.addsitedir`, or load a module by file path (`spec_from_file_location`, `SourceFileLoader`,
`runpy.run_path`): a path import sidesteps the package doors and the budgets. Shared code goes
into a governed package the script imports normally. Every measured path-import site
fails directly; no baseline can permit it. The one narrow exception (a within-skill `__file__` guard) is in `path_imports.py`.

### Rule 9: ambient state (inject what is read to decide)

`scripts/structure/ambient_state/`: module-level state read to decide and free-floating
background work fail, except what the closed lists in `allowlist.py` allow. Frozen as
`path::rule:name -> site count`, exact and shrink-only like Rules 4-6; also governs `schedules/`.

### Structure budgets: 800 lines per file, 20 direct entries per directory

Budgets cover the governed packages in `_SCAN_DIRS`, plus tests/ and scripts/.
Direct entries are .py/.pyi files and subdirectories with content; hidden entries,
symlinks, __pycache__, migrations subtrees, and a subdirectory holding nothing
else (a local leftover CI never checks out) are excluded. Each directory is
independent. A `docs/` or `tests/` layer without `__init__.py` takes no slot in its
parent's budget, and a `tests/` layer has no entry cap of its own (see
`scripts/structure/budgets/directory_budget.py`); its files keep the 800-line ceiling.
AST rules retain their governed-package scope.

File, directory, complexity and nesting budgets have no exemptions. Every
selected violation fails, including after a file or function rename.

scripts/structure/baseline/**/*.json temporarily tracks remaining site exemptions.
Ordinary component folders organize storage; baseline_shards is its sole reader.
The guard compares them with the base revision and forbids added keys or raised
counts, including when a lint rule version changes. Delete resolved entries.
An explicit file target also checks its parent directory. The guard always runs.
Function quality covers the budget scope: CC >=15 is hard, CC 10-14 warns,
and control-flow nesting >5 is hard.

"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import (  # noqa: E402 — standalone script
    ambient_state,
    baseline_shards,
    lint_common,
    locality,
    path_imports,
)
from scripts.structure.budgets import (  # noqa: E402 — standalone script
    directory_budget,
    quality_budget,
)

_HARD_CEILING = 800
# Baseline sections whose frozen `path::target` site counts must match reality exactly.
_SITE_SECTIONS = (*locality.EXTERNAL_SECTIONS, ambient_state.SECTION)
_MEASURED_SECTIONS = (*locality.SECTIONS, path_imports.SECTION, ambient_state.SECTION)
_DIRECTORY_CEILING = 20

# AST rules track [tool.importlinter] root_packages; budgets also cover tooling/tests.
_SCAN_DIRS = lint_common.FRAMEWORK_DIRS
_AST_DIRS = ambient_state.SCOPE  # _SCAN_DIRS plus schedules/, which only Rule 9 governs
_STRUCTURE_DIRS = (*_SCAN_DIRS, "tests", "scripts")

# Rule 3 allowlist — modules that may call machine_role(), each with the
# question the call answers ("what do I serve" vs "where does this run").
# A call site not listed here fails the lint; a listed module whose calls
# disappear fails too (stale entry). See the module docstring.
_MACHINE_ROLE_ALLOWED: dict[str, str] = {
    "cli/commands/lifecycle/_temporary_stop.py": "Which selected local services and data plane does this unit own during explicit stop? No execution is routed elsewhere.",
    "ops/agent_pause/__init__.py": "Does this unit serve an agent host whose admitted cohort and actual continuation completion must be verified before local shutdown?",
    "base/cluster/machine.py": "defines machine_role() and its capability wrappers is_gateway()/is_agent_runner() — the implementation itself",
    "base/telemetry/observability.py": "does this process serve the gateway capability whose LGTM marker governs telemetry (what do I serve)",
    "services/supervision/healthchecks/otel_collector.py": "does this unit own the LGTM collector healthcheck, preserving pure-runner relay behavior (what do I serve)",
    "cli/commands/lifecycle/start.py": "which daemons do I bring up (what do I serve)",
    "cli/commands/_repo.py": "resolve this host's capability set, None when unset, for stop/status/converge (what do I serve)",
    "cli/commands/observability/trace.py": "which recovery ingress does this host serve: gateway-local Tempo or a pure-runner relay target (what do I serve)",
    "services/agent_runner/agent_ops/_boot.py": "what do I advertise in register_self (what do I serve)",
    "ops/inventory.py": "capability guard: inventory ops are agent-runner-only (what do I serve)",
    "gateway/routers/config.py": "for the gateway itself, local role is authoritative (what do I serve)",
}


def _machine_role_calls(tree: ast.AST) -> list[int]:
    """Line numbers of `machine_role(...)` call sites in a parsed module."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "machine_role"
        ):
            hits.append(node.lineno)
    return hits


# Files allowed to use `if TYPE_CHECKING:` — real circular import or heavy
# optional dependency. Add an entry with a one-line reason.
_TYPE_CHECKING_ALLOWED: frozenset[str] = frozenset(
    {
        # PEP 562 lazy re-export: the node set is a heavy import on paths (the
        # exec child) that never use the names; TYPE_CHECKING restores the real
        # signatures for static checkers without putting them back on the import
        # path (task #3585).
        "agent/graph/__init__.py",
        # The exec child builds an `AvaContext` from its request envelope: the handle types
        # (psycopg pool, redis bus, chat model) are annotation-only fields it never holds, and
        # importing them would put the whole DB / LM stack on its boot path.
        "base/agents/context/__init__.py",
        # The same exec-child boot path: a ClientSet builds psycopg / redis / httpx clients on first
        # use only, so its client types are annotation-only.
        "base/agents/context/clients.py",
        # LM provider registration surface: the chat-model stack is a heavy
        # import on the exec-child boot path, which never uses the type
        # (annotation-only references; task #3633).
        "base/lm/provider_api.py",
        "base/lm/factory.py",
        "ava_builtins/plugins/lm_alibaba/provider.py",
        "ava_builtins/plugins/lm_anthropic/provider.py",
        "ava_builtins/plugins/lm_deepseek/provider.py",
        "ava_builtins/plugins/lm_google/provider.py",
        "ava_builtins/plugins/lm_moonshot/provider.py",
        "ava_builtins/plugins/lm_openai/provider.py",
        "ava_builtins/plugins/lm_xiaomi/provider.py",
        "ava_builtins/plugins/lm_zhipu/provider.py",
        # LangChain message-stack trim on the same registration path: message
        # types appear in annotations only, or (pricing) import at the call
        # site of a runtime isinstance (task #3633).
        "base/lm/stop.py",
        "base/lm/pricing/__init__.py",
        "base/agents/messages/kwargs.py",
        # Exec-child boot path (`_run_code` -> sdk_telemetry): ToolMessage is
        # runtime-only (imported at the isinstance call site), BaseMessage
        # annotation-only (task #3633).
        "base/agents/sdk/telemetry.py",
        # Boot-lite facade: the names are served at runtime by the lite latch;
        # TYPE_CHECKING keeps `from base.config import X` consumers resolving
        # without an eager import that would rebuild the config chain the lite
        # facade defers (task #3621).
        "base/config/__init__.py",
        # Boot-path trim: the skill-index stack is a heavy import every exec
        # child pays; `SkillFile` is annotation-only on the mount helpers
        # (script/module-level imports removed for task #3816).
        "ava/skills/__init__.py",
        # Boot-path trim: the fleet plugin (autoloaded into every exec child)
        # keeps psycopg off its module import graph — `psycopg` is
        # annotation-only here, imported at the raise sites (task #3816).
        "ava_builtins/plugins/ava_fleet/task_registry.py",
        "ava_builtins/plugins/ava_fleet/_task_update.py",
        "base/agents/tasks/reparent.py",
        "base/agents/tasks/rules.py",
    }
)


def _type_checking_violations(tree: ast.Module) -> list[int]:
    """Line numbers of `if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` blocks."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        name = (
            test.id
            if isinstance(test, ast.Name)
            else test.attr
            if isinstance(test, ast.Attribute)
            else None
        )
        if name == "TYPE_CHECKING":
            hits.append(node.lineno)
    return hits


def _scan_file(path: Path, rel_path: str, tree: ast.Module | None = None) -> list[tuple[int, str]]:
    """Return AST violations as [(lineno, message), ...]."""
    if lint_common.is_test_path(rel_path):
        return []  # tests are outside the AST rules (module docstring)
    if tree is None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []  # unreadable entry (e.g. a dangling symlink) or binary content
        tree = ast.parse(text, filename=rel_path)
    out: list[tuple[int, str]] = []

    if rel_path not in _TYPE_CHECKING_ALLOWED:
        for lineno in _type_checking_violations(tree):
            out.append(
                (
                    lineno,
                    "`if TYPE_CHECKING:` is banned — import at top level. "
                    "App deps are always on the runtime path and LangGraph/Pydantic "
                    "introspect type hints at runtime (deferred imports NameError). "
                    "Real circular-import / heavy-dep cases: refactor, or add this file "
                    "to _TYPE_CHECKING_ALLOWED in scripts/lint/code_structure.py with a reason.",
                )
            )

    role_calls = _machine_role_calls(tree)
    if rel_path in _MACHINE_ROLE_ALLOWED:
        if not role_calls:
            out.append(
                (
                    1,
                    f"stale machine_role() allowlist entry — {rel_path} no longer calls "
                    "machine_role(); remove it from _MACHINE_ROLE_ALLOWED in "
                    "scripts/lint/code_structure.py (the list must match reality, "
                    "issue #216).",
                )
            )
    elif role_calls:
        for lineno in role_calls:
            out.append(
                (
                    lineno,
                    "machine_role() may only be called from modules in "
                    "_MACHINE_ROLE_ALLOWED (scripts/lint/code_structure.py) — the "
                    "gateway is the single routing point and no operation may branch "
                    "on role (user ruling 2026-08-21, issue #216). Add the module "
                    "deliberately with the question the call answers, or route the "
                    "operation to the gateway instead.",
                )
            )

    out.extend(locality.allowlist_errors(tree, rel_path, _SCAN_DIRS))
    return out


def _in_scan_scope(rel_path: str) -> bool:
    return any(rel_path == d or rel_path.startswith(f"{d}/") for d in _SCAN_DIRS)


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def _ancestors_within(path: Path, scope: Path) -> set[Path]:
    """Every directory from `path`'s own up to `scope`: a new subpackage adds an entry to each
    ancestor's budget, not just to its parent's."""
    return {a for a in path.parents if a == scope or scope in a.parents}


def _budget_targets(targets: list[Path]) -> tuple[set[Path], set[Path]]:
    """Collect files and independently checked directories without following links."""
    files: set[Path] = set()
    directories: set[Path] = set()
    visited: set[Path] = set()

    def visit(directory: Path) -> None:
        if directory in visited:
            return
        visited.add(directory)
        directories.add(directory)
        for entry in directory_budget.entries(directory):
            if entry.is_dir():
                visit(entry)
            elif entry.is_file() and entry.suffix == ".py":
                files.add(entry)

    for target in targets:
        for scope in (_REPO_ROOT / name for name in _STRUCTURE_DIRS):
            selected = directory_budget.selected_under(target, scope, _REPO_ROOT)
            if selected is None:
                continue
            if selected.is_dir():
                visit(selected)
            elif selected.is_file():
                directories.update(_ancestors_within(selected, scope))
                if selected.suffix == ".py":
                    files.add(selected)
    return files, directories


def _parse_baseline(
    shards: dict[str, str], *, renames: dict[str, str] | None = None, historical: bool = False
) -> dict[str, dict[str, int]]:
    """Validate remaining site exemptions; retired budgets cannot be reintroduced.

    The comparison revision may still contain empty budget or locality sections from
    before retirement. They convey no allowance and are discarded after validation.
    """
    retired = (
        ("directories", "files", *quality_budget.QUALITY_SECTIONS, *locality.STRICT_SECTIONS)
        if historical
        else ()
    )
    baseline = baseline_shards.merge(shards, (*_SITE_SECTIONS, *retired))
    for kind in retired:
        if baseline.pop(kind):
            raise ValueError(f"retired {kind} baseline must be empty")
    for kind in _SITE_SECTIONS:
        scope = ambient_state.SCOPE if kind == ambient_state.SECTION else _SCAN_DIRS
        locality.validate_entries(kind, baseline[kind], _carried_scope(scope, renames or {}))
    return baseline


def _carried_scope(scope: tuple[str, ...], renames: dict[str, str]) -> tuple[str, ...]:
    """`scope` plus every top-level directory a detected rename moves files into it from."""
    carried = {
        PurePosixPath(old).parts[0] for old, new in renames.items() if new.split("/")[0] in scope
    }
    return tuple(sorted(set(scope) | carried))


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — local git query, no shell
        ["git", "-C", str(_REPO_ROOT), *args], capture_output=True, text=True, check=False
    )


def _baseline_base() -> str:
    if "LINT_STRUCTURE_BASELINE_BASE" in os.environ:
        ref = os.environ["LINT_STRUCTURE_BASELINE_BASE"]
        for args in (
            ("merge-base", "--", "HEAD", ref),
            ("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"),
        ):
            result = _git(*args)
            if result.returncode == 0:
                return result.stdout.strip()
        raise ValueError(f"LINT_STRUCTURE_BASELINE_BASE={ref!r} cannot resolve to a commit")
    if _git("rev-parse", "--verify", "origin/main^{commit}").returncode == 0:
        result = _git("merge-base", "HEAD", "origin/main")
        if result.returncode == 0:
            return result.stdout.strip()
        print("note: origin/main merge-base unavailable; falling back to HEAD", file=sys.stderr)
    return "HEAD"


def _rename_map(base: str) -> dict[str, str]:
    """Old -> new paths for the renames git -M detects between `base` and the working tree.

    A detected rename carries remaining frozen site keys to the new path: move the
    baseline entries with it — remove the old key, add the new one with the same
    value — and the guard accepts the edit. The frozen values still cap the new
    path: a raise stays a violation and a new key without a paired removal stays
    an addition. A rewrite git no longer detects as a rename is evaluated fresh.
    With -B, a module whose old path now holds a total rewrite still counts as
    moved (git reports that pair as a copy, `C`, of the rewritten source).
    `-l0` lifts `diff.renameLimit`: a package-wide move edits thousands of files at
    once, and past the limit git silently stops pairing edited moves.
    """
    result = _git(
        "diff", "-M", "-B", "-l0", "--name-status", "--diff-filter=RC", "--no-color", base
    )
    if result.returncode:
        print(f"note: rename map unavailable ({base} diff failed)", file=sys.stderr)
        return {}
    renames: dict[str, str] = {}
    for line in result.stdout.splitlines():
        status, _, rest = line.partition("\t")
        old, separator, new = rest.partition("\t")
        if status.startswith(("R", "C")) and separator and old and new:
            renames[old] = new
    return renames


def _rename_map_or_empty() -> dict[str, str]:
    """The rename map for the guard's comparison base; an unresolvable base maps nothing."""
    try:
        return _rename_map(_baseline_base())
    except ValueError:
        # The baseline guard reports the unresolvable base itself.
        return {}


def _remap_renamed_keys(entries: dict[str, int], renames: dict[str, str]) -> dict[str, int]:
    """Carry remaining site keys when Git detects a file move."""
    remapped: dict[str, int] = {}
    for name, value in entries.items():
        path, separator, target_name = name.partition("::")
        target = f"{renames.get(path, path)}{separator}{target_name}"
        remapped[target] = min(remapped.get(target, value), value)
    return remapped


def _renamed_to(name: str, renames: dict[str, str]) -> str | None:
    return renames.get(name.partition("::")[0])


def _section_guard(
    kind: str,
    current: dict[str, int],
    previous: dict[str, int],
    *,
    renames: dict[str, str] | None = None,
) -> list[str]:
    errors: list[str] = []
    additions = current.keys() - previous.keys()
    paired = kind in locality.SECTIONS
    if paired:
        additions = set(locality.unpaired_additions(current, previous))
    for name in sorted(additions):
        moved_to = _renamed_to(name, renames or {})
        if moved_to is not None:
            new_key = moved_to + name[len(name.partition("::")[0]) :]
            errors.append(
                f"{baseline_shards.shard_path(kind, name)}: {kind} entry {name} was not migrated "
                f"after its file moved to {moved_to} — move this key to {new_key} in "
                f"{baseline_shards.shard_path(kind, new_key)} with the same value"
            )
            continue
        rule = (
            "baseline is shrink-only"
            if not paired
            else "added key without a same-file removal of the same private name: a split, "
            "move or swap cannot carry a frozen site — route it through the door or owner"
        )
        errors.append(
            f"{baseline_shards.shard_path(kind, name)}: added {kind} entry {name} — {rule}"
        )
    for name in sorted(current.keys() & previous.keys()):
        if current[name] > previous[name]:
            errors.append(
                f"{baseline_shards.shard_path(kind, name)}: raised {kind} entry {name} from {previous[name]} "
                f"to {current[name]} — baseline is shrink-only"
            )
    return errors


def _baseline_guard(
    baseline: dict[str, dict[str, int]], *, renames: dict[str, str] | None = None
) -> list[str]:
    try:
        base = _baseline_base()
        shards = baseline_shards.read_at(_REPO_ROOT, base)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        return [f"{baseline_shards.SHARD_DIR}: {exc}"]
    if shards is None:
        print(
            f"note: baseline guard skipped: {base} predates {baseline_shards.SHARD_DIR}",
            file=sys.stderr,
        )
        return []
    try:
        previous = _parse_baseline(shards, renames=renames, historical=True)
    except ValueError as exc:
        return [f"{baseline_shards.SHARD_DIR}: invalid base baseline ({base}): {exc}"]
    try:
        rules_was, rules_now = baseline_shards.read_rules(_REPO_ROOT, base)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        return [f"{baseline_shards.SHARD_DIR}: invalid rule versions: {exc}"]
    errors: list[str] = []
    for kind, entries in baseline.items():
        was, now = rules_was.get(kind, 1), rules_now.get(kind, 1)
        if now < was:
            errors.append(
                f"{baseline_shards.SHARD_DIR}/{baseline_shards.RULES_FILE}: "
                f"{kind} rule version went back from {was} to {now}"
            )
        errors.extend(
            _section_guard(
                kind,
                entries,
                _remap_renamed_keys(previous[kind], renames or {}),
                renames=renames,
            )
        )
    return errors


def _check_budgets(targets: list[Path]) -> list[str]:
    files, directories = _budget_targets(targets)
    errors: list[str] = []
    for path in sorted(files):
        try:
            count = len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue  # Preserve the shared lint contract for unreadable members.
        name = path.relative_to(_REPO_ROOT).as_posix()
        if count > _HARD_CEILING:
            errors.append(
                f"{name}:{count}: file is {count} lines, over the {_HARD_CEILING}-line hard ceiling — split it"
            )
    for path in sorted(directories):
        if directory_budget.is_tests_layer(path):
            continue  # no entry cap of its own (module docstring)
        count = sum(
            directory_budget.counts_toward_budget(entry) for entry in directory_budget.entries(path)
        )
        name = path.relative_to(_REPO_ROOT).as_posix()
        if count > _DIRECTORY_CEILING:
            errors.append(
                f"{name}: directory has {count} direct entries, over the {_DIRECTORY_CEILING}-entry cap — split it"
            )
    return errors


def _ast_rule_files(argv: list[str]) -> set[Path]:
    # Preserve the AST rules' original resolved-target scope, including aliases.
    targets = [Path(a).resolve() for a in argv] if argv else [_REPO_ROOT / d for d in _AST_DIRS]
    return set(_iter_py_files(targets))


def _collect_locality(
    tree: ast.Module, rel: str, sites: dict[str, locality.Sites], scanned: set[str]
) -> None:
    scanned.add(rel)
    for kind, found in locality.measure(tree, rel, _SCAN_DIRS, _REPO_ROOT).items():
        sites[kind].update(found)
    sites[path_imports.SECTION].update(path_imports.measure(tree, rel))


def _governed(path: Path, rel: str, ast_files: set[Path]) -> tuple[bool, bool]:
    """(Rules 1, 3-6; Rule 9) govern this file: Rule 9 also reaches schedules/."""
    in_ast = path in ast_files
    return in_ast and _in_scan_scope(rel), in_ast and ambient_state.in_scope(rel)


def _check_ast_and_quality(
    argv: list[str],
    targets: list[Path],
    baseline: dict[str, dict[str, int]],
    *,
    full: bool,
    renames: dict[str, str] | None = None,
) -> list[str]:
    files, _ = _budget_targets(targets)
    ast_files = _ast_rule_files(argv)
    locality.reset_caches()
    measurements: dict[str, dict[str, int]] = {kind: {} for kind in quality_budget.QUALITY_SECTIONS}
    sites: dict[str, locality.Sites] = {kind: {} for kind in _MEASURED_SECTIONS}
    scanned: set[str] = set()
    errors: list[str] = []
    for path in sorted(files | ast_files):
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            continue
        ast_rules, ambient = _governed(path, rel, ast_files)
        if path not in files and not (ast_rules or ambient):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        tree = ast.parse(text, filename=rel)
        if ast_rules:
            errors.extend(
                f"{rel}:{line}: {message}" for line, message in _scan_file(path, rel, tree)
            )
            _collect_locality(tree, rel, sites, scanned)
        if ambient:
            errors.extend(ambient_state.collect(tree, rel, _REPO_ROOT, sites, scanned))
        if path in files:
            for kind, values in quality_budget.measure_quality(tree, rel).items():
                measurements[kind].update(values)
    errors.extend(quality_budget.quality_errors(measurements))
    errors.extend(
        locality.site_errors(
            sites, baseline, scanned=scanned, repo_root=_REPO_ROOT, renames=renames
        )
    )
    errors.extend(locality.missing_allowlist_errors(_REPO_ROOT))
    errors.extend(ambient_state.missing_allowlist_errors(_REPO_ROOT))
    quality_budget.render_warnings(measurements["complexity"], full=full)
    return errors


def _changed_targets(only: list[str] | None) -> list[str]:
    """The `--only` changed files as explicit targets; none (the full scan) when the tooling changed."""
    scope = lint_common.changed_scope(only, _REPO_ROOT)
    return [] if scope is None else [str(_REPO_ROOT / rel) for rel in sorted(scope)]


def _parse_args(argv: list[str]) -> tuple[list[str], bool, bool]:
    """(explicit targets, unfold complexity warnings, nothing to judge) from the command line."""
    full = "--complexity-warnings-full" in argv
    argv, only = lint_common.split_only([a for a in argv if a != "--complexity-warnings-full"])
    targets = [*argv, *_changed_targets(only)]
    return targets, full, only == [] and not targets


def main(argv: list[str] | None = None) -> int:
    argv, full, nothing_changed = _parse_args(argv if argv is not None else sys.argv[1:])
    if nothing_changed:
        return 0
    missing = [arg for arg in argv if not Path(arg).exists()]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    # Keep symlinks visible to the budget collector so it can exclude them.
    targets = (
        [Path(os.path.abspath(a)) for a in argv]  # noqa: PTH100 — normalize without following symlinks
        if argv
        else [_REPO_ROOT / d for d in _STRUCTURE_DIRS]
    )
    try:
        baseline = _parse_baseline(baseline_shards.read_worktree(_REPO_ROOT))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"{baseline_shards.SHARD_DIR}: invalid baseline: {exc}", file=sys.stderr)
        return 1
    renames = _rename_map_or_empty()
    errors = _baseline_guard(baseline, renames=renames)
    errors.extend(_check_budgets(targets))
    errors.extend(_check_ast_and_quality(argv, targets, baseline, full=full, renames=renames))
    for error in errors:
        print(error)
    if errors:
        print(
            f"\n{len(errors)} hard violations. See scripts/lint/code_structure.py for the rules.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
