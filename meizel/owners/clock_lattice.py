"""Forbid lattice-vocabulary timing constants outside the clock-lattice modules.

Run: `.venv/bin/python -m meizel.owners.clock_lattice [path ...]` (defaults to
scanning the non-test source dirs; an explicit path that does not exist is an
error (stderr + exit 1) rather than a bare traceback). Also run automatically
via pre-commit hook.

## Why

The clock lattice (`base/deploy/timing.py`) is the single authority for every timing
constant that must hold an ORDER relative to its neighbours — boot stall < launch
confirm < boot budget < reap grace, NO_PROGRESS < schedule stall, the lease TTLs, the
controller scan cadence, the wedged derivation. The orderings are load-bearing:
the 2026-07-30 spawn incident happened because a launch-confirm window was raised
without its neighbouring reap grace, and the relation existed only in prose.

A bare `_SOME_REAP_GRACE_S = 100` in a new module is that incident's seedling —
the name carries lattice vocabulary, so it reads as part of the lattice, but
nothing knows its neighbours. This lint is the code-level guard: lattice
vocabulary may only appear where the lattice can see it.

## The rule

A module-level constant whose name contains lattice vocabulary (`STALL`, `GRACE`,
`REAP`, `BUDGET`, `WEDGED`, `NO_PROGRESS`, `LAUNCH_CONFIRM`, `LEASE_TTL`,
`LEASE_RENEW`, `SCAN_INTERVAL`, `REAP_INTERVAL`) must be one of:

1. **Defined in a lattice family module** — `base/deploy/timing.py`,
   `base/deploy/progress_timeout.py`, `base/native_process/stop_timing.py`,
   `base/deploy/schedule_timing.py`. These are the lattice's homes; registering
   a new clock there and in `CLOCKS` is the correct way to add one.
2. **An alias of a registered clock** — the assignment's value is a bare
   reference to a clock registered in `base.deploy.timing.CLOCKS`
   (e.g. `_ROLLOUT_STALL_TIMEOUT_S: float = NO_PROGRESS_TIMEOUT_S`). The value is
   still defined once; the alias is just a local name.
3. **Explicitly exempt** in `INDEPENDENT_CLOCKS` below — the constant is either
   not a clock at all (a flag name, a SQL key, a collection) or a genuinely
   independent clock with no lattice neighbour, each with a stated reason.

The allowlists must stay current: a full scan (no path arguments, as pre-commit
and CI run it) also fails when a `FAMILY_MODULES` file or an
`INDEPENDENT_CLOCKS` file is gone, or an exempted constant is no longer defined
at module level in its file — an exemption for a deleted symbol would silently
pre-approve whatever next takes that name.

## Values bound to a clock name

The same vocabulary also governs the value a name is *bound* to, wherever the binding is written:
a module constant (above), the default of a dataclass field, a keyword argument at a call, or an
ordinary assignment (`name = 5.0`, `self.name = 5.0`). A name that carries lattice vocabulary as whole
snake_case words (`stall`, `grace`, `reap`, `budget`, `wedged`, `no_progress`, `launch_confirm`,
`lease_ttl`, `lease_renew`, `scan_interval`, `reap_interval`) and is bound to a number literal (or an
expression of literals only) is a definition of a clock, so it may appear only in a family
module or under an `INDEPENDENT_CLOCKS` entry (`path::name`). Anything else defines nothing and is not
checked: a bare reference to another name or attribute (an alias, or a config field that carries the
value somebody else gave it), a field without a default, a default of `None`. Moving the
literal to the call site (`HealthConfig(startup_grace_s=30)`) is a definition at that site.

Scope: non-test code only (tests monkeypatch clocks smaller on purpose).
Settings fields are class-body definitions in `base/config/` and are the
operator-overridable configuration authority, not module constants — they are not
scanned, and `base/deploy/timing.py` registers them by reference.

Error format `file:line: <reason>` + non-zero exit.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from meizel import allowances, lint_common
from meizel.roles import packages

_REPO_ROOT = Path(__file__).resolve().parents[2]

SCAN_DIRS = packages("application", "tooling")

_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

# Lattice vocabulary: a module-level constant carrying any of these substrings is
# presumed to be part of the clock lattice until proven otherwise.
_LATTICE_TERMS = (
    "STALL",
    "GRACE",
    "REAP",
    "BUDGET",
    "WEDGED",
    "NO_PROGRESS",
    "LAUNCH_CONFIRM",
    "LEASE_TTL",
    "LEASE_RENEW",
    "SCAN_INTERVAL",
    "REAP_INTERVAL",
)

_POLICY = allowances.load(_REPO_ROOT)

# The lattice family modules: lattice vocabulary may be DEFINED here (and only here). Listed in
# meizel.policy.toml under [clock-lattice].
FAMILY_MODULES = _POLICY.names("clock-lattice", "family-modules")

_CONST_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


# Registered clock names, for the alias rule (rule 2): the assignment's value must
# be a bare reference to one of these. Read from `base/deploy/timing.py` once, at
# import, with a pure AST parse (no import of the app's settings stack), so the
# lint runs anywhere the source tree is present — the same zero-dependency shape
# as every other scripts/ lint.
def _parse_registered_clocks() -> frozenset[str]:
    tree = ast.parse((_REPO_ROOT / "base" / "deploy" / "timing.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        if (
            isinstance(target, ast.Name)
            and target.id == "CLOCKS"
            and isinstance(node.value, ast.Dict)
        ):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    names.add(key.value)
    return frozenset(names)


REGISTERED_CLOCKS: frozenset[str] = _parse_registered_clocks()


def _registered_clock_names() -> frozenset[str]:
    return REGISTERED_CLOCKS


# Explicit exemptions: `(relative_path, NAME)` -> why this lattice-vocabulary constant may live
# outside the family modules. Either it is not a clock at all (a flag, a key, a collection), or it
# is a genuinely independent clock with no lattice neighbour. Every entry states which, in
# meizel.policy.toml under [clock-lattice.independent-clocks] (`path::NAME`).
INDEPENDENT_CLOCKS: dict[tuple[str, str], str] = {
    (path, name): why
    for site, why in _POLICY.reasons("clock-lattice", "independent-clocks").items()
    for path, _, name in [site.partition("::")]
}


def _is_unregistered_clock(rel: str, name: str, value: ast.expr | None) -> bool:
    """A lattice-vocabulary constant outside the family modules, not exempt and not an alias."""
    if not _CONST_NAME.match(name) or not any(term in name for term in _LATTICE_TERMS):
        return False
    if rel in FAMILY_MODULES:
        return False  # rule 1: the lattice's homes
    if (rel, name) in INDEPENDENT_CLOCKS:
        return False  # rule 3: explicit exemption
    # rule 2: alias of a registered clock
    return not (isinstance(value, ast.Name) and value.id in _registered_clock_names())


def _carries_vocabulary(name: str) -> bool:
    """Lattice vocabulary as whole snake_case words (so `install` does not carry `stall`)."""
    padded = f"_{name.lower()}_"
    return any(f"_{term.lower()}_" in padded for term in _LATTICE_TERMS)


def _literal(node: ast.expr | None) -> bool:
    """A number, or an expression made only of numbers."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, (ast.USub, ast.UAdd)) and _literal(node.operand)
    if isinstance(node, ast.BinOp):
        return _literal(node.left) and _literal(node.right)
    return False


def _bound_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _bound_names(element)]
    return []


def _assignment_bindings(
    node: ast.Assign | ast.AnnAssign, *, module_level: bool
) -> list[tuple[str, int]]:
    """The vocabulary-carrying names one assignment binds to a literal, minus module constants."""
    if not _literal(node.value):
        return []
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [
        (name, node.lineno)
        for target in targets
        for name in _bound_names(target)
        if _carries_vocabulary(name) and not (module_level and _CONST_NAME.match(name))
    ]


def _keyword_bindings(call: ast.Call) -> list[tuple[str, int]]:
    return [
        (keyword.arg, keyword.value.lineno)
        for keyword in call.keywords
        if keyword.arg and _literal(keyword.value) and _carries_vocabulary(keyword.arg)
    ]


def literal_bindings(tree: ast.Module) -> list[tuple[str, int]]:
    """(name, line) of every vocabulary-carrying name bound to a literal, outside module constants.

    The bindings are field defaults, assignments in any scope and call keyword arguments; a
    module-level `UPPER_CASE` constant is left to the constant rules above.
    """
    found: list[tuple[str, int]] = []
    module_level = {id(node) for node in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            found.extend(_assignment_bindings(node, module_level=id(node) in module_level))
        elif isinstance(node, ast.Call):
            found.extend(_keyword_bindings(node))
    return found


def scan_file(path: Path, *, independent: Mapping[tuple[str, str], str] | None = None) -> list[str]:
    try:
        rel = path.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []  # unreadable entry (dangling symlink / non-UTF-8), or a syntax error the compiler owns
    errors: list[str] = []
    exempt = INDEPENDENT_CLOCKS if independent is None else independent
    if rel not in FAMILY_MODULES:
        errors.extend(
            f"{path}:{line}: {name} — a lattice-vocabulary name bound to a literal outside the "
            "lattice family modules; give it no value here (take it from a config the root builds) "
            "or define the clock in a family module and register it in CLOCKS"
            for name, line in literal_bindings(tree)
            if (rel, name) not in exempt
        )
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and _is_unregistered_clock(rel, target.id, node.value):
                errors.append(
                    f"{path}:{node.lineno}: {target.id} — lattice-vocabulary clock outside "
                    "the lattice family modules; define it in base/deploy/timing.py (and "
                    "register it in CLOCKS) or make it an alias of a registered clock"
                )
    return errors


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def stale_allowlist_entries(
    independent: Mapping[tuple[str, str], str] | None = None,
) -> list[str]:
    """Allowlist entries whose file or exempted constant no longer exists."""
    exempt = INDEPENDENT_CLOCKS if independent is None else independent
    errors: list[str] = []
    for rel in FAMILY_MODULES:
        if not (_REPO_ROOT / rel).is_file():
            errors.append(f"{rel}: stale FAMILY_MODULES entry — the file no longer exists")
    for rel, name in exempt:
        path = _REPO_ROOT / rel
        if not path.is_file():
            errors.append(
                f"{rel}: stale INDEPENDENT_CLOCKS entry {name} — the file no longer exists"
            )
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue  # unreadable or a syntax error the compiler owns: cannot judge
        if name.islower():
            if name not in {bound for bound, _ in literal_bindings(tree)}:
                errors.append(
                    f"{rel}: stale INDEPENDENT_CLOCKS entry {name} — nothing binds {name} to a "
                    "literal there any more; drop the exemption"
                )
        elif name not in _module_level_names(tree):
            errors.append(
                f"{rel}: stale INDEPENDENT_CLOCKS entry {name} — no module-level {name} "
                "is defined there any more; drop the exemption"
            )
    return errors


def _scan(paths: list[Path], scope: frozenset[str] | None = None) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if path.is_dir():
            for p in sorted(path.rglob("*.py")):
                try:
                    rel = p.relative_to(_REPO_ROOT).as_posix()
                except ValueError:
                    rel = p.as_posix()
                if any(pat.search(rel) for pat in _TEST_PATTERNS):
                    continue
                if scope is not None and rel not in scope:
                    continue
                errors.extend(scan_file(p))
        elif path.suffix == ".py":
            errors.extend(scan_file(path))
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, only = lint_common.split_only(argv)
    if argv:
        missing = [arg for arg in argv if not Path(arg).exists()]
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    paths = (
        [Path(a).resolve() for a in argv] if argv else lint_common.scan_roots(_REPO_ROOT, SCAN_DIRS)
    )
    # `--only` (the commit hook) judges the changed files under the default scope; the
    # lattice's own family modules are inputs: editing one re-judges every file.
    scope = lint_common.changed_scope(only, _REPO_ROOT, inputs=FAMILY_MODULES)
    errors = _scan(paths, scope)
    if not argv:
        errors.extend(stale_allowlist_entries())
    for err in errors:
        print(err, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
