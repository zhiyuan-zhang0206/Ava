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
the type — are listed under `[type-checking.files]` in `meizel.policy.toml` with a one-line reason.

### Rule 3: retired ambient machine-role reader

Machine capabilities belong to an explicit MachineIdentity supplied by the
composition root. The retired machine_role() call is rejected in every governed
production module; there is no per-file allowance. Capability guards read the
owner's role() and retain the single gateway routing contract (issue #216).

### Rule 4: retired

Reaching another package's `_`-prefixed module or name is reported by meizel
(`private-name`, `private-module`, with the `meizel.toml` exports deciding what is a
public door); see `meizel/docs/checker/checker.ava.okf.md`. This script no longer measures it.

### Rule 5: single decision owners (locality)

`meizel/owners/decisions.py:DECISIONS` names design decisions that have exactly
one owning module; any other module making that decision is a bypass. Today:
`postgres-dial` — a psycopg connect (module, class, or `from psycopg import
connect`) or a construction of a psycopg_pool pool or of this repo's own
`*ConnectionPool` subclass belongs to `base/db/connections.py`, which owns the
transport posture. A site that genuinely cannot go through the owner goes in that
decision's `[single-owner.<decision>]` table in `meizel.policy.toml` with a one-line reason; an allowed module that stops
bypassing, or no longer exists, fails as stale.

Rule 5 has no baseline allowance. Every measured decision-owner bypass fails directly. Owner resolution stays in scripts/structure/locality.py and
the decision contracts live in meizel/owners/decisions.py; see docs/conventions/python-conventions.md.

### Rule 6: no path imports under ava_builtins/ (package doors)

`meizel/language/path_imports.py`: a skill or plugin module may not edit `sys.path`, call
`site.addsitedir`, or load a module by file path (`spec_from_file_location`, `SourceFileLoader`,
`runpy.run_path`): a path import sidesteps the package doors and the budgets. Shared code goes
into a governed package the script imports normally. Every measured path-import site
fails directly; no baseline can permit it. The one narrow exception (a within-skill `__file__` guard) is in `path_imports.py`.

### Rule 9: ambient state (inject what is read to decide)

`meizel/state/ambient_state/`: module-level state read to decide and free-floating
background work fail, except what the closed lists in `allowlist.py` allow. Frozen as
`path::rule:name -> site count`, exact and shrink-only like Rules 5-6; also governs `schedules/`.

### Structure budgets: 800 lines per file, 20 direct entries per directory

Every non-root directory has at most 20 direct Git-tracked entries. Files of every
suffix, hidden entries, docs, tests, migrations, symlinks and gitlinks each count
one slot. A directory counts once in its parent and is independently checked at
any depth. Symlink targets and untracked local artifacts are not traversed.
The repository root alone has no entry cap. File and function budgets retain the
existing Python scope: `_SCAN_DIRS`, tests/ and scripts/. AST rules retain their
governed-package scope.

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
from collections.abc import Callable
from pathlib import Path, PurePosixPath

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from meizel import lint_common  # noqa: E402 — standalone script
from meizel.budgets import limits  # noqa: E402 — standalone script
from meizel.language import path_imports, type_checking  # noqa: E402 — standalone script
from meizel.owners import decisions  # noqa: E402 — standalone script
from meizel.roles import packages  # noqa: E402 - standalone script
from meizel.state import ambient_state, machine_role  # noqa: E402 — standalone script
from scripts.structure import baseline_shards, locality  # noqa: E402 — standalone script
from scripts.structure.budgets import quality_budget  # noqa: E402 — standalone script

__all__ = ["main"]

# Baseline sections whose frozen `path::target` site counts must match reality exactly.
_SITE_SECTIONS = (*locality.EXTERNAL_SECTIONS, ambient_state.SECTION)
_MEASURED_SECTIONS = (*locality.SECTIONS, path_imports.SECTION, ambient_state.SECTION)

# AST rules track the application packages; budgets also cover tooling/tests.
_SCAN_DIRS = lint_common.FRAMEWORK_DIRS
_AST_DIRS = ambient_state.SCOPE  # _SCAN_DIRS plus schedules/, which only Rule 9 governs
_STRUCTURE_DIRS = packages("application", "tests", "tooling")


def _scan_file(tree: ast.Module, rel_path: str) -> list[tuple[int, str]]:
    """Return AST violations as [(lineno, message), ...]."""
    if lint_common.is_test_path(rel_path):
        return []  # tests are outside the AST rules (module docstring)
    out: list[tuple[int, str]] = []
    out.extend(type_checking.violations(tree, rel_path))
    out.extend(machine_role.violations(tree))
    out.extend(decisions.allowlist_errors(tree, rel_path, _SCAN_DIRS))
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


def _parse_baseline(
    shards: dict[str, str], *, renames: dict[str, str] | None = None
) -> dict[str, dict[str, int]]:
    """Validate the remaining site exemptions; any other section is an error."""
    baseline = baseline_shards.merge(shards, _SITE_SECTIONS)
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


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — local git query, no shell
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False
    )


def _baseline_base(repo_root: Path, explicit: str | None) -> str:
    """Resolve an explicit snapshot or the actual merge base with origin/main."""
    ref = explicit if explicit is not None else os.environ.get("LINT_STRUCTURE_BASELINE_BASE")
    if ref is not None:
        result = _git(repo_root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        if result.returncode:
            source = "baseline_base" if explicit is not None else "LINT_STRUCTURE_BASELINE_BASE"
            raise ValueError(f"{source}={ref!r} cannot resolve to a commit")
    else:
        result = _git(repo_root, "merge-base", "--", "HEAD", "origin/main")
        result.check_returncode()
    return result.stdout.strip()


def _rename_map(repo_root: Path, base: str) -> dict[str, str]:
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
        repo_root,
        "diff",
        "-M",
        "-B",
        "-l0",
        "--name-status",
        "--diff-filter=RC",
        "--no-color",
        base,
    )
    result.check_returncode()
    renames: dict[str, str] = {}
    for line in result.stdout.splitlines():
        status, _, rest = line.partition("\t")
        old, separator, new = rest.partition("\t")
        if status.startswith(("R", "C")) and separator and old and new:
            renames[old] = new
    return renames


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
        errors.append(
            f"{baseline_shards.shard_path(kind, name)}: added {kind} entry {name} — baseline is shrink-only"
        )
    for name in sorted(current.keys() & previous.keys()):
        if current[name] > previous[name]:
            errors.append(
                f"{baseline_shards.shard_path(kind, name)}: raised {kind} entry {name} from {previous[name]} "
                f"to {current[name]} — baseline is shrink-only"
            )
    return errors


def _baseline_guard(
    baseline: dict[str, dict[str, int]],
    *,
    repo_root: Path,
    base: str,
    renames: dict[str, str] | None = None,
) -> list[str]:
    try:
        shards = baseline_shards.read_at(repo_root, base)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        return [f"{baseline_shards.SHARD_DIR}: {exc}"]
    try:
        previous = _parse_baseline(shards, renames=renames)
    except ValueError as exc:
        return [f"{baseline_shards.SHARD_DIR}: invalid base baseline ({base}): {exc}"]
    try:
        rules_was, rules_now = baseline_shards.read_rules(repo_root, base)
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


def _ast_rule_files(argv: list[str], repo_root: Path) -> set[Path]:
    # Preserve the AST rules' original resolved-target scope, including aliases.
    targets = [Path(a).resolve() for a in argv] if argv else [repo_root / d for d in _AST_DIRS]
    return set(_iter_py_files(targets))


def _collect_locality(
    tree: ast.Module, rel: str, sites: dict[str, locality.Sites], scanned: set[str]
) -> None:
    scanned.add(rel)
    for kind, found in locality.measure(tree, rel, _SCAN_DIRS).items():
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
    repo_root: Path,
    renames: dict[str, str] | None = None,
    parse_ast: Callable[..., ast.Module],
) -> list[str]:
    files = limits.budget_files(targets, repo_root, _STRUCTURE_DIRS)
    ast_files = _ast_rule_files(argv, repo_root)
    measurements: dict[str, dict[str, int]] = {kind: {} for kind in quality_budget.QUALITY_SECTIONS}
    sites: dict[str, locality.Sites] = {kind: {} for kind in _MEASURED_SECTIONS}
    scanned: set[str] = set()
    errors: list[str] = []
    for path in sorted(files | ast_files):
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        ast_rules, ambient = _governed(path, rel, ast_files)
        if path not in files and not (ast_rules or ambient):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        tree = parse_ast(text, filename=rel)
        if ast_rules:
            errors.extend(f"{rel}:{line}: {message}" for line, message in _scan_file(tree, rel))
            _collect_locality(tree, rel, sites, scanned)
        if ambient:
            errors.extend(ambient_state.collect(tree, rel, repo_root, sites, scanned))
        if path in files:
            for kind, values in quality_budget.measure_quality(tree, rel).items():
                measurements[kind].update(values)
    errors.extend(quality_budget.quality_errors(measurements))
    errors.extend(
        locality.site_errors(sites, baseline, scanned=scanned, repo_root=repo_root, renames=renames)
    )
    errors.extend(decisions.missing_allowlist_errors(repo_root))
    errors.extend(ambient_state.missing_allowlist_errors(repo_root))
    quality_budget.render_warnings(measurements["complexity"], full=full)
    return errors


def _changed_targets(only: list[str] | None, repo_root: Path) -> list[str]:
    """The `--only` changed files as explicit targets; none (the full scan) when the tooling changed."""
    scope = lint_common.changed_scope(only, repo_root)
    return [] if scope is None else [str(repo_root / rel) for rel in sorted(scope)]


def _parse_args(argv: list[str], repo_root: Path) -> tuple[list[str], bool, bool]:
    """(explicit targets, unfold complexity warnings, nothing to judge) from the command line."""
    full = "--complexity-warnings-full" in argv
    argv, only = lint_common.split_only([a for a in argv if a != "--complexity-warnings-full"])
    targets = [*argv, *_changed_targets(only, repo_root)]
    return targets, full, only == [] and not targets


def _budget_scopes(argv: list[str], repo_root: Path) -> tuple[list[Path], list[Path]]:
    """Explicit paths share one scope; a full run counts directories across the repo."""
    if argv:
        # Keep tracked symlinks lexical: count their names without following targets.
        selected = [Path(os.path.abspath(a)) for a in argv]  # noqa: PTH100 — normalize without following symlinks
        return selected, selected
    return [repo_root / directory for directory in _STRUCTURE_DIRS], [repo_root]


def main(
    argv: list[str] | None = None,
    *,
    repo_root: Path | None = None,
    baseline_base: str | None = None,
    parse_ast: Callable[..., ast.Module],
) -> int:
    """Run the gate for CLI arguments against an explicit checkout and comparison commit.

    Omitted arguments use sys.argv; target paths retain cwd-relative CLI semantics.
    The comparison defaults to LINT_STRUCTURE_BASELINE_BASE, then the merge base
    of HEAD and origin/main.
    Diagnostics use stdout/stderr and the return value is the CLI exit status.
    """
    repo_root = repo_root.resolve() if repo_root is not None else _REPO_ROOT
    argv, full, nothing_changed = _parse_args(argv if argv is not None else sys.argv[1:], repo_root)
    if nothing_changed:
        return 0
    missing = [arg for arg in argv if not os.path.lexists(arg)]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    targets, directory_targets = _budget_scopes(argv, repo_root)
    try:
        baseline = _parse_baseline(baseline_shards.read_worktree(repo_root))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"{baseline_shards.SHARD_DIR}: invalid baseline: {exc}", file=sys.stderr)
        return 1
    try:
        base = _baseline_base(repo_root, baseline_base)
        renames = _rename_map(repo_root, base)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"{baseline_shards.SHARD_DIR}: cannot establish baseline comparison: {exc}")
        return 1
    errors = _baseline_guard(baseline, repo_root=repo_root, base=base, renames=renames)
    try:
        errors.extend(limits.check(targets, directory_targets, repo_root, _STRUCTURE_DIRS))
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"cannot read tracked directory structure: {exc}", file=sys.stderr)
        return 1
    errors.extend(
        _check_ast_and_quality(
            argv,
            targets,
            baseline,
            full=full,
            repo_root=repo_root,
            renames=renames,
            parse_ast=parse_ast,
        )
    )
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
    sys.exit(main(parse_ast=ast.parse))
