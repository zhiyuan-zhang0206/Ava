"""Ambient state: module-level state that code reads to decide, and background work no one owns.

Rule 9 in scripts/lint/code_structure.py. Inject what is read to decide; a
write-only facade may stay global. For anything a module holds at import — an
instance, a container, a `global` slot, a `ContextVar`, an import-time call or
read — ask whether anything reads it to decide what to do. If so it belongs to a
component built in a composition root and handed in, not to the module. A logger,
meter or tracer is written through and read by nothing, so it may stay a global.

`scan.py` finds the sites (its docstring lists every rule and the known
gaps), `allowlist.py` holds the closed lists with a reason per entry, and
the frozen sites live in the `ambient_state` section of the
scripts/structure/baseline/ shards as `path::rule:name -> site count`. The rule ids:

- state: `ambient-instance`, `ambient-container`, `contextvar`, `global-rebind`,
  `foreign-rebind`, `class-level-container`, `hidden-singleton`, `hidden-cache`;
- import-time effects: `import-time-call`, `import-time-read`, `host-fact`;
- configuration reads: `settings-read` — a module of a slice-governed package
  (`allowlist.SLICED_PACKAGES`, see `sliced.py`) other than its composition root imports the
  process-global `settings`;
- database and bundle wiring: `ambient-db` (a package in `allowlist.DB_HANDLE_PACKAGES` dials
  from the live settings instead of taking a `Database`, see `dbhandle.py`) and `bundle-leak` (a
  `@root_bundle` class named outside its defining module, see `bundle.py`);
- `ambient-clock`: a package in `allowlist.CLOCK_PACKAGES` builds the cluster clock
  (`Clock.from_settings()`) outside the roots named for it;
- `ambient-bus`: a package in `allowlist.BUS_PACKAGES` builds the event bus
  (`EventBus.from_settings()`) outside the roots named for it;
- `ambient-endpoint`: a package in `allowlist.ENDPOINT_PACKAGES` builds the endpoint table
  (`ServiceEndpoints.from_settings()`) outside the roots named for it;
- free-floating background work: `asyncio-task`, `thread` (keyed by the enclosing
  function). Background work must be durable or re-derivable from durable state and
  run as its own service loop; use a per-iteration `async with asyncio.TaskGroup()`
  for bounded parallelism, never a free-floating `create_task` or thread.

Scope: the governed packages plus `schedules/`. Tests, `__main__.py`, an
`if __name__ == "__main__":` block and skill scripts (`ava_builtins/skills/**`,
`ava_builtins/plugins/*/**/skills/**`, which run as programs, not importable
modules) are outside it. There is no inline exemption.

Like Rules 4-6 the frozen counts must match reality in both directions, and
against the base revision the section is shrink-only: a new key, or a raised
count, is refused; a fixed site must be removed from the baseline. A rename or a
split cannot carry a frozen site. Introducing this lint does not permit a new
baseline entry; comparisons always use the base revision's entries.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from scripts.structure import lint_common
from scripts.structure.ambient_state import allowlist as allow
from scripts.structure.ambient_state import (
    bundle,
    busrule,
    clockrule,
    dbhandle,
    endpointrule,
    scan,
    sliced,
)

SECTION = "ambient_state"
# The rule module path used by integration-test repository fixtures.
LINT = "scripts/structure/ambient_state/__init__.py"
SCOPE = (*lint_common.FRAMEWORK_DIRS, "schedules")
Sites = dict[str, list[int]]

_SKILL_SCRIPTS = re.compile(r"^ava_builtins/(skills/|plugins/[^/]+/(.*/)?skills/)")

_STATE_FIX = (
    "inject what is read to decide: build it in a composition root and hand it to the "
    "component that uses it. A write-only facade (logger, meter, tracer) may stay "
    "global — add its file to SINK_FACADES in scripts/structure/ambient_state/allowlist.py with a reason"
)
_BACKGROUND_FIX = (
    "background work must be durable or re-derivable from durable state and run as its own "
    "service loop; use a per-iteration `async with asyncio.TaskGroup()` for bounded "
    "parallelism, never a free-floating `create_task` or thread"
)
_FIXES: dict[str, str] = {
    scan.INSTANCE: f"module-level instance — {_STATE_FIX}",
    scan.CONTAINER: f"module-level mutable container — {_STATE_FIX}",
    scan.CONTEXTVAR: (
        "LangGraph Runtime[AvaContext] already carries graph-run dependencies — "
        "do not duplicate them with a ContextVar; outside the graph, pass "
        "the caller's context explicitly instead of adding an ambient carrier"
    ),
    scan.GLOBAL_REBIND: f"a function rebinds a module global — {_STATE_FIX}",
    scan.FOREIGN: "rebinds an attribute of another module — give the owner a setter or inject the value",
    scan.CLASS_CONTAINER: "mutable container shared through a class attribute — make it per-instance state",
    scan.SINGLETON: f"a zero-argument cache is a hidden singleton — {_STATE_FIX}; a memoized pure derivation goes in ALLOWED with a reason",
    scan.CACHE: "a memoized function is state unless it is pure — a pure derivation goes in ALLOWED in scripts/structure/ambient_state/allowlist.py with a reason",
    scan.CALL: "a call that runs at import (a registry fill or side effect) — register from a composition root, not at import",
    dbhandle.AMBIENT_DB: f"an ambient database dial in a package that holds a Database handle — {dbhandle.FIX}",
    clockrule.AMBIENT_CLOCK: f"the cluster clock built outside a root — {clockrule.FIX}",
    busrule.AMBIENT_BUS: f"the event bus built outside a root — {busrule.FIX}",
    endpointrule.AMBIENT_ENDPOINT: f"the endpoint table built outside a root — {endpointrule.FIX}",
    bundle.BUNDLE_LEAK: f"a root bundle named outside its composition root — {bundle.FIX}",
    sliced.SETTINGS_READ: f"reads the global configuration in a sliced package — {sliced.FIX}",
    scan.READ: "reads settings, the environment, the clock or the filesystem at import — read it where it is used, or inject it",
    scan.HOST: "a platform constant computed at import — inject a `Platform` instead of recomputing the fact per module",
    scan.TASK: f"a free-floating task — {_BACKGROUND_FIX}",
    scan.THREAD: f"a free-floating thread — {_BACKGROUND_FIX}",
}


def in_scope(rel: str) -> bool:
    """Whether the rule governs this repo-relative path."""
    return (
        rel.split("/", 1)[0] in SCOPE
        and not lint_common.is_test_path(rel)
        and not rel.endswith("__main__.py")
        and _SKILL_SCRIPTS.match(rel) is None
    )


_BACKGROUND_WORK = frozenset({scan.TASK, scan.THREAD})


def _is_facade_state(rel: str, hit: scan.Hit) -> bool:
    """State inside a sink facade is let through; background work started there is not."""
    return rel in allow.SINK_FACADES and hit.rule not in _BACKGROUND_WORK


def site_key(rel: str, hit: scan.Hit) -> str:
    return f"{rel}::{hit.rule}:{hit.name}"


def _hits(tree: ast.Module, rel: str, repo_root: Path) -> list[scan.Hit]:
    if not in_scope(rel):
        return []
    return [
        *scan.scan(tree, rel, repo_root),
        *sliced.hits(tree, rel),
        *dbhandle.hits(tree, rel),
        *endpointrule.hits(tree, rel),
        *busrule.hits(tree, rel),
        *clockrule.hits(tree, rel),
        *bundle.hits(tree, repo_root),
    ]


def measure(tree: ast.Module, rel: str, repo_root: Path) -> Sites:
    """The site keys of one module with the closed lists applied, `path::rule:name -> [line]`."""
    sites: Sites = {}
    for hit in _hits(tree, rel, repo_root):
        key = site_key(rel, hit)
        if key not in allow.ALLOWED and not _is_facade_state(rel, hit):
            sites.setdefault(key, []).append(hit.line)
    return sites


def _listed_in(rel: str) -> dict[str, str]:
    """The ALLOWED entries that name sites of this file."""
    prefix = f"{rel}::"
    return {key: why for key, why in allow.ALLOWED.items() if key.startswith(prefix)}


def allowlist_errors(tree: ast.Module, rel: str, repo_root: Path) -> list[tuple[int, str]]:
    """A listed exemption whose site is no longer reported is stale."""
    listed = _listed_in(rel)
    if not listed and rel not in allow.SINK_FACADES:
        return []
    hits = _hits(tree, rel, repo_root)
    present = {site_key(rel, hit) for hit in hits}
    errors = [
        (1, f"stale ambient_state list entry {key} — the site is gone; remove it from {_LIST_FILE}")
        for key in sorted(listed)
        if key not in present
    ]
    if rel in allow.SINK_FACADES and not any(_is_facade_state(rel, hit) for hit in hits):
        errors.append(
            (
                1,
                f"stale SINK_FACADES entry — the file holds no module-level state; remove it from {_LIST_FILE}",
            )
        )
    return errors


_LIST_FILE = "scripts/structure/ambient_state/allowlist.py"


def _missing_roots(repo_root: Path, table: str, registry: dict[str, frozenset[str]]) -> list[str]:
    return [
        f"{_LIST_FILE}:1: stale {table} entry {package} — {path} does not exist; fix or remove it"
        for package, roots in sorted(registry.items())
        for path in sorted(roots)
        if not (repo_root / path).is_file()
    ]


def missing_allowlist_errors(repo_root: Path) -> list[str]:
    """A listed file or function that no longer exists is stale too."""
    listed_paths = {
        *allow.SINK_FACADES,
        *(key.partition("::")[0] for key in allow.ALLOWED),
    }
    errors = [
        f"{path}:1: stale ambient_state list entry — the file no longer exists; remove it from {_LIST_FILE}"
        for path in sorted(listed_paths)
        if not (repo_root / path).is_file()
    ]
    for package, roots in sorted(allow.SLICED_PACKAGES.items()):
        for path in (f"{package}/config.py", *sorted(roots)):
            if not (repo_root / path).is_file():
                errors.append(
                    f"{_LIST_FILE}:1: stale SLICED_PACKAGES entry {package} — {path} does not exist; fix or remove it"
                )
    for table, registry in (
        ("DB_HANDLE_PACKAGES", allow.DB_HANDLE_PACKAGES),
        ("ENDPOINT_PACKAGES", allow.ENDPOINT_PACKAGES),
        ("BUS_PACKAGES", allow.BUS_PACKAGES),
        ("CLOCK_PACKAGES", allow.CLOCK_PACKAGES),
    ):
        errors += _missing_roots(repo_root, table, registry)
    for callee in sorted(allow.PURE_REPO_CALLEES):
        owner, _, name = callee.rpartition(".")
        path = repo_root.joinpath(*owner.split(".")).with_suffix(".py")
        if not path.is_file() or f"def {name}(" not in path.read_text(encoding="utf-8"):
            errors.append(
                f"{_LIST_FILE}:1: stale PURE_REPO_CALLEES entry {callee} — no such function; remove it"
            )
    return errors


def is_target(target: str) -> bool:
    """Whether a baseline key's `rule:name` part names a rule this module knows."""
    rule, separator, name = target.partition(":")
    return rule in _FIXES and bool(separator and name)


def site_message(target: str) -> str:
    """The failure text for a new or grown site, by the rule named in its `rule:name` key."""
    rule, _, name = target.partition(":")
    return f"`{rule}` `{name}`: {_FIXES[rule]}"


def collect(
    tree: ast.Module,
    rel: str,
    repo_root: Path,
    sites: dict[str, Sites],
    scanned: set[str],
) -> list[str]:
    """Add one module's measured sites to `sites` and mark it scanned; return its stale-list
    errors. The one call the gate makes per governed file."""
    sites[SECTION].update(measure(tree, rel, repo_root))
    scanned.add(rel)
    return [f"{rel}:{line}: {message}" for line, message in allowlist_errors(tree, rel, repo_root)]
