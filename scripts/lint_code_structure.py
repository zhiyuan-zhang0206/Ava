"""Structural lints that keep the codebase legible to agents: no `TYPE_CHECKING`
import-folding, role-call allowlisting, and frozen structure/function budgets.

Run: `.venv/bin/python scripts/lint_code_structure.py [path ...]` (defaults to the
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
definition. `locality.FRAMEWORK_TIERS` names the packages whose `_` marks another
axis by documented convention: a private module or package directly under one
(today `ava/_*.py`: hidden from agents, open to the framework) is exempt, while a
private name inside an agent-facing module (`ava.files._x`) is not. A module
alias rebound anywhere in the file (a parameter such as `self`, a local) is not
followed. Files under a tests/ directory are exempt.

### Rule 5: single decision owners (locality)

`scripts/structure/locality.py:DECISIONS` names design decisions that have exactly
one owning module; any other module making that decision is a bypass. Today:
`postgres-dial` — a psycopg connect (module, class, or `from psycopg import
connect`) or a construction of a psycopg_pool pool or of this repo's own
`*ConnectionPool` subclass belongs to `shared/db_connections.py`, which owns the
transport posture. A site that genuinely cannot go through the owner goes in that
decision's `allowed` map with a one-line reason; an allowed module that stops
bypassing, or no longer exists, fails as stale.

Rules 4 and 5 freeze today's sites in the `private_imports` / `owner_bypasses`
baseline sections as `path::target -> site count`. Unlike the budgets, the
frozen counts must match reality exactly: a new or grown site is a violation,
and a removed one fails until its entry is lowered or deleted, so a fixed
reach-in cannot silently return. Against the base revision they are shrink-only:
a new key needs a same-file removal of the SAME private name with equal or
greater value (its owner module moved), and git -M renames carry keys. A file
split, a move to another file, or a swap for a different private name cannot
carry a frozen site — fix the site instead. Why locality:
conventions/python-conventions.md.

### Structure budgets: 800 lines per file, 20 direct entries per directory

Budgets cover the governed packages in `_SCAN_DIRS`, plus tests/ and scripts/.
Direct entries are .py/.pyi files and subdirectories; hidden entries, symlinks,
__pycache__, and migrations subtrees are excluded. Each directory is independent.
AST rules retain their governed-package scope.

scripts/structure/baseline.json freezes existing over-limit counts. New or growing
violations fail; the baseline itself may only lose entries or lower values versus
the configured base (or merge-base with origin/main, falling back to HEAD).
After splitting, shrink the relevant baseline values or remove fixed entries
by hand. Explicit targets restrict budget checks to the selected files/directories;
a file also checks its parent directory. The baseline guard always runs.

Function budgets: Radon 6.0.1 CC >=15 is hard, 10-14 warns; control-flow nesting
>5 is hard. The complexity/nesting baseline sections use path::qualname keys.
Same-file one-to-one removals may cover renamed keys with equal or lower values.
Renames git -M detects carry their frozen keys: migrate the baseline entries to
the new path — remove the old key, add the new one with the same value — and the
guard accepts the edit. The frozen values still cap the new path: a raise, or an
unpaired new key, stays a violation. Moves whose edit breaks rename detection (a
rewrite, not an import-path touch-up) are evaluated fresh under the new path.
Use --complexity-warnings-full anywhere in argv to unfold all warning file counts.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import locality  # noqa: E402 — standalone script
from scripts.structure import quality_budget as quality  # noqa: E402 — standalone script

_HARD_CEILING = 800
_DIRECTORY_CEILING = 20
_BASELINE_PATH = "scripts/structure/baseline.json"

# AST rules track [tool.importlinter] root_packages; budgets also cover tooling/tests.
_SCAN_DIRS = (
    "agent",
    "ava",
    "ava_builtins",
    "gateway",
    "shared",
    "services",
    "ops",
    "cli",
)
_STRUCTURE_DIRS = (*_SCAN_DIRS, "tests", "scripts")

# Rule 3 allowlist — modules that may call machine_role(), each with the
# question the call answers ("what do I serve" vs "where does this run").
# A call site not listed here fails the lint; a listed module whose calls
# disappear fails too (stale entry). See the module docstring.
_MACHINE_ROLE_ALLOWED: dict[str, str] = {
    "cli/commands/_temporary_stop.py": "Which selected local services and data plane does this unit own during explicit pause/stop? No execution is routed elsewhere.",
    "ops/agent_pause.py": "Does this unit serve an agent host whose admitted cohort and actual continuation completion must be verified before local shutdown?",
    "ops/cluster_pause.py": "Does this unit serve an agent host whose live daemon identity must answer before a pre-stop hold release restores local posture (what do I serve)",
    "cli/commands/_maintenance.py": "Which services/data plane does this explicitly local, DB-offline-capable stop/start own? Fleet transport is operator-coordinated.",
    "shared/machine.py": "defines machine_role() and its capability wrappers is_gateway()/is_agent_runner() — the implementation itself",
    "shared/observability.py": "does this process serve the gateway capability whose LGTM marker governs telemetry (what do I serve)",
    "services/healthchecks/otel_collector.py": "does this unit own the LGTM collector healthcheck, preserving pure-runner relay behavior (what do I serve)",
    "cli/commands/start.py": "which daemons do I bring up (what do I serve)",
    "cli/commands/_repo.py": "resolve this host's capability set, None when unset, for stop/status/converge (what do I serve)",
    "cli/commands/_gateway_ready.py": "audit this host's role for the readiness report (what do I serve)",
    "cli/commands/trace.py": "which recovery ingress does this host serve: gateway-local Tempo or a pure-runner relay target (what do I serve)",
    "services/agent_ops/_boot.py": "what do I advertise in register_self (what do I serve)",
    "ops/ops_inventory.py": "capability guard: inventory ops are agent-runner-only (what do I serve)",
    "gateway/routers/config.py": "for the gateway itself, local role is authoritative (what do I serve)",
    "cli/commands/_release_inventory.py": "verified installed image reads the real annotated service roster for the unit receipt (read-only, WHEEL_RUNTIME-guarded; no serve decision)",
    "cli/commands/_release_services.py": "verified candidate updater rechecks the local annotated roster before any service stop or start (read-only, WHEEL_RUNTIME-guarded; no routing decision)",
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
        # LM provider registration surface: the chat-model stack is a heavy
        # import on the exec-child boot path, which never uses the type
        # (annotation-only references; task #3633).
        "shared/lm/provider_api.py",
        "shared/lm/factory.py",
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
        "shared/lm/stop.py",
        "shared/lm/pricing.py",
        "shared/message_kwargs.py",
        # Exec-child boot path (`_run_code` -> sdk_telemetry): ToolMessage is
        # runtime-only (imported at the isinstance call site), BaseMessage
        # annotation-only (task #3633).
        "shared/sdk_telemetry.py",
        # Boot-lite facade: the names are served at runtime by the lite latch;
        # TYPE_CHECKING keeps `from shared.config import X` consumers resolving
        # without an eager import that would rebuild the config chain the lite
        # facade defers (task #3621).
        "shared/config/__init__.py",
        # Boot-path trim: the skill-index stack is a heavy import every exec
        # child pays; `SkillFile` is annotation-only on the mount helpers
        # (script/module-level imports removed for task #3816).
        "ava/skills.py",
        # Boot-path trim: the fleet plugin (autoloaded into every exec child)
        # keeps psycopg off its module import graph — `psycopg` is
        # annotation-only here, imported at the raise sites (task #3816).
        "ava_builtins/plugins/ava_fleet/task_registry.py",
        "ava_builtins/plugins/ava_fleet/_task_update.py",
        "shared/tasks/task_reparent.py",
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
                    "to _TYPE_CHECKING_ALLOWED in scripts/lint_code_structure.py with a reason.",
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
                    "scripts/lint_code_structure.py (the list must match reality, "
                    "issue #216).",
                )
            )
    elif role_calls:
        for lineno in role_calls:
            out.append(
                (
                    lineno,
                    "machine_role() may only be called from modules in "
                    "_MACHINE_ROLE_ALLOWED (scripts/lint_code_structure.py) — the "
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


def _budget_entries(directory: Path) -> list[Path]:
    return [
        entry
        for entry in directory.iterdir()
        if not entry.is_symlink()
        and not entry.name.startswith(".")
        and entry.name != "__pycache__"
        and not (entry.name == "migrations" and entry.is_dir())
    ]


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
        for entry in _budget_entries(directory):
            if entry.is_dir():
                visit(entry)
            elif entry.is_file() and entry.suffix == ".py":
                files.add(entry)

    for target in targets:
        for scope in (_REPO_ROOT / name for name in _STRUCTURE_DIRS):
            if target == scope or scope in target.parents:
                selected = target
            elif target in scope.parents:
                selected = scope
            else:
                continue
            relative = selected.relative_to(_REPO_ROOT)
            if any(
                part.startswith(".") or part in {"__pycache__", "migrations"}
                for part in relative.parts
            ) or any(path.is_symlink() for path in (selected, *selected.parents)):
                continue
            if selected.is_dir():
                visit(selected)
            elif selected.is_file():
                directories.add(selected.parent)
                if selected.suffix == ".py":
                    files.add(selected)
    return files, directories


def _parse_baseline(text: str, *, allow_legacy: bool = False) -> dict[str, dict[str, int]]:
    baseline = json.loads(text)
    budgets = {"directories", "files", *quality.QUALITY_SECTIONS}
    sections = budgets | set(locality.SECTIONS)
    legacy = [budgets, {"directories", "files"}] if allow_legacy else []
    if not isinstance(baseline, dict) or set(baseline) not in [sections, *legacy]:
        raise ValueError(f"expected exactly the sections {sorted(sections)}")
    for kind in quality.QUALITY_SECTIONS:
        if kind in baseline:
            quality.validate_quality_entries(kind, baseline[kind], _STRUCTURE_DIRS)
    for kind in locality.SECTIONS:
        if kind in baseline:
            locality.validate_entries(kind, baseline[kind], _SCAN_DIRS)
    _validate_structure_entries(baseline)
    return baseline


def _validate_structure_entries(baseline: dict[str, dict[str, int]]) -> None:
    for kind, ceiling in (("directories", _DIRECTORY_CEILING), ("files", _HARD_CEILING)):
        entries = baseline[kind]
        if not isinstance(entries, dict):
            raise ValueError(f"'{kind}' must be an object")  # noqa: TRY004 — invalid JSON schema
        for name, count in entries.items():
            path = Path(name)
            if (
                not name
                or not path.parts
                or path.is_absolute()
                or path.as_posix() != name
                or ".." in path.parts
                or path.parts[0] not in _STRUCTURE_DIRS
                or (kind == "files" and path.suffix != ".py")
                or type(count) is not int
                or count <= ceiling
            ):
                raise ValueError(
                    f"invalid {kind} entry {name!r}: expected a scoped path and integer > {ceiling}"
                )


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

    A detected rename carries its frozen baseline keys to the new path: move the
    baseline entries with it — remove the old key, add the new one with the same
    value — and the guard accepts the edit. The frozen values still cap the new
    path: a raise stays a violation and a new key without a paired removal stays
    an addition. A rewrite git no longer detects as a rename is evaluated fresh.
    """
    result = _git("diff", "-M", "--name-status", "--diff-filter=R", "--no-color", base)
    if result.returncode:
        print(f"note: rename map unavailable ({base} diff failed)", file=sys.stderr)
        return {}
    renames: dict[str, str] = {}
    for line in result.stdout.splitlines():
        status, _, rest = line.partition("\t")
        old, separator, new = rest.partition("\t")
        if status.startswith("R") and separator and old and new:
            renames[old] = new
    return renames


def _rename_map_or_empty() -> dict[str, str]:
    """The rename map for the guard's comparison base; an unresolvable base maps nothing."""
    try:
        return _rename_map(_baseline_base())
    except ValueError:
        # The baseline guard reports the unresolvable base itself.
        return {}


def _remap_renamed_keys(
    kind: str, entries: dict[str, int], renames: dict[str, str]
) -> dict[str, int]:
    """Carry each renamed file's entry over to its new path (files/complexity/nesting)."""
    if not renames or kind == "directories":
        return entries
    remapped: dict[str, int] = {}
    for name, value in entries.items():
        if kind == "files":
            target = renames.get(name, name)
        else:
            path, separator, qualname = name.partition("::")
            target = f"{renames.get(path, path)}{separator}{qualname}"
        if target in remapped:
            # Unreachable for a valid baseline; keep the stricter (smaller) cap.
            remapped[target] = min(remapped[target], value)
        else:
            remapped[target] = value
    return remapped


def _renamed_to(kind: str, name: str, renames: dict[str, str]) -> str | None:
    """The new path of a stale entry's renamed file (files/complexity/nesting), if known."""
    if kind == "files":
        return renames.get(name)
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
    paired = kind in quality.QUALITY_SECTIONS or kind in locality.SECTIONS
    if kind in quality.QUALITY_SECTIONS:
        additions = set(quality.unpaired_additions(current, previous))
    elif kind in locality.SECTIONS:
        additions = set(locality.unpaired_additions(current, previous))
    for name in sorted(additions):
        moved_to = _renamed_to(kind, name, renames or {})
        if moved_to is not None:
            errors.append(
                f"{_BASELINE_PATH}: {kind} entry {name} was not migrated after its file "
                f"moved to {moved_to} — move this key to the new path with the same value"
            )
            continue
        rule = (
            "baseline is shrink-only"
            if not paired
            else "added key without a paired same-file removal of equal or greater value"
            if kind in quality.QUALITY_SECTIONS
            else "added key without a same-file removal of the same private name: a split, "
            "move or swap cannot carry a frozen site — route it through the door or owner"
        )
        errors.append(f"{_BASELINE_PATH}: added {kind} entry {name} — {rule}")
    for name in sorted(current.keys() & previous.keys()):
        if current[name] > previous[name]:
            errors.append(
                f"{_BASELINE_PATH}: raised {kind} entry {name} from {previous[name]} "
                f"to {current[name]} — baseline is shrink-only"
            )
    return errors


def _baseline_guard(
    baseline: dict[str, dict[str, int]], *, renames: dict[str, str] | None = None
) -> list[str]:
    try:
        base = _baseline_base()
    except ValueError as exc:
        return [f"{_BASELINE_PATH}: {exc}"]
    result = _git("show", f"{base}:{_BASELINE_PATH}")
    if result.returncode:
        print(
            f"note: baseline guard skipped: git {base}:{_BASELINE_PATH} unavailable",
            file=sys.stderr,
        )
        return []
    try:
        previous = _parse_baseline(result.stdout, allow_legacy=True)
    except ValueError as exc:
        return [f"{_BASELINE_PATH}: invalid base baseline ({base}): {exc}"]
    errors: list[str] = []
    for kind, entries in baseline.items():
        if kind not in previous:
            print(
                f"note: {kind} baseline guard skipped: section absent at base {base}",
                file=sys.stderr,
            )
        else:
            errors.extend(
                _section_guard(
                    kind,
                    entries,
                    _remap_renamed_keys(kind, previous[kind], renames or {}),
                    renames=renames,
                )
            )
    return errors


def _budget_error(value: int, ceiling: int, name: str, baseline: dict[str, int]) -> str | None:
    if value <= ceiling:
        return None
    if name not in baseline:
        return "new violation, not in the baseline — split it"
    if value > baseline[name]:
        return f"grew above its frozen baseline value ({baseline[name]}) — split it"
    return None


def _check_budgets(targets: list[Path], baseline: dict[str, dict[str, int]]) -> list[str]:
    files, directories = _budget_targets(targets)
    errors: list[str] = []
    for path in sorted(files):
        try:
            count = len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue  # Preserve the shared lint contract for unreadable members.
        name = path.relative_to(_REPO_ROOT).as_posix()
        error = _budget_error(count, _HARD_CEILING, name, baseline["files"])
        if error:
            errors.append(
                f"{name}:{count}: file is {count} lines, over the {_HARD_CEILING}-line hard ceiling: {error}"
            )
    for path in sorted(directories):
        count = sum(
            entry.is_dir() or (entry.is_file() and entry.suffix in {".py", ".pyi"})
            for entry in _budget_entries(path)
        )
        name = path.relative_to(_REPO_ROOT).as_posix()
        error = _budget_error(count, _DIRECTORY_CEILING, name, baseline["directories"])
        if error:
            errors.append(
                f"{name}: directory has {count} direct entries, over the {_DIRECTORY_CEILING}-entry cap: {error}"
            )
    return errors


def _ast_rule_files(argv: list[str]) -> set[Path]:
    # Preserve the AST rules' original resolved-target scope, including aliases.
    targets = [Path(a).resolve() for a in argv] if argv else [_REPO_ROOT / d for d in _SCAN_DIRS]
    return set(_iter_py_files(targets))


def _collect_locality(
    tree: ast.Module, rel: str, sites: dict[str, locality.Sites], scanned: set[str]
) -> None:
    scanned.add(rel)
    for kind, found in locality.measure(tree, rel, _SCAN_DIRS, _REPO_ROOT).items():
        sites[kind].update(found)


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
    measurements: dict[str, dict[str, int]] = {kind: {} for kind in quality.QUALITY_SECTIONS}
    sites: dict[str, locality.Sites] = {kind: {} for kind in locality.SECTIONS}
    scanned: set[str] = set()
    errors: list[str] = []
    for path in sorted(files | ast_files):
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            continue
        ast_rules = path in ast_files and _in_scan_scope(rel)
        if path not in files and not ast_rules:
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
        if path in files:
            for kind, values in quality.measure_quality(tree, rel).items():
                measurements[kind].update(values)
    errors.extend(quality.quality_errors(measurements, baseline, renames=renames))
    errors.extend(
        locality.site_errors(
            sites, baseline, scanned=scanned, repo_root=_REPO_ROOT, renames=renames
        )
    )
    errors.extend(locality.missing_allowlist_errors(_REPO_ROOT))
    quality.render_warnings(measurements["complexity"], full=full)
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    full = "--complexity-warnings-full" in argv
    argv = [arg for arg in argv if arg != "--complexity-warnings-full"]
    if argv:
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
        baseline = _parse_baseline((_REPO_ROOT / _BASELINE_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"{_BASELINE_PATH}: invalid baseline: {exc}", file=sys.stderr)
        return 1
    renames = _rename_map_or_empty()
    errors = _baseline_guard(baseline, renames=renames)
    errors.extend(_check_budgets(targets, baseline))
    errors.extend(_check_ast_and_quality(argv, targets, baseline, full=full, renames=renames))
    for error in errors:
        print(error)
    if errors:
        print(
            f"\n{len(errors)} hard violations. See scripts/lint_code_structure.py for the rules.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
