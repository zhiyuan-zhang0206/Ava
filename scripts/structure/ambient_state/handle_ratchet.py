"""Ratchet on the library layer's self-built database and event-bus handles.

Run: `.venv/bin/python scripts/structure/ambient_state/handle_ratchet.py [--write]`. Whole-repo; also runs via pre-commit.

A package in `DB_HANDLE_PACKAGES` takes its `Database` from a composition root and is
policed site by site (structure Rule 9, `ambient-db`). The library packages are not
governed yet: they still dial through the process-default shim (`connect` / `pool` /
`async_pool` / `direct_db_url` / a pool-less `write_transaction`) or build their own
`Database.from_settings()`; the same holds for an `EventBus.from_settings()` built outside
`BUS_PACKAGES`. This lint counts those sites per package, freezes the counts in
`handle_ratchet_baseline.json`, and lets them only fall:

- a package above its frozen count fails, listing the sites — new code takes a handle;
- a package below its frozen count fails until the baseline is lowered (`--write`), so the
  ratchet cannot hide room to regress;
- against the base revision a frozen count may not rise or appear, so the baseline file
  cannot be used to smuggle growth in.

Threading a handle into a library package (one package per commit) drops its count; a package
at zero leaves the baseline and, when it is ready, joins `DB_HANDLE_PACKAGES` / `BUS_PACKAGES`.

Error format `path:line: <reason>` + non-zero exit.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import ambient_state, lint_common  # noqa: E402 - standalone script
from scripts.structure.ambient_state import busrule, dbhandle  # noqa: E402 - standalone script

BASELINE = "scripts/structure/ambient_state/handle_ratchet_baseline.json"
SHIM = "shim"
SELF_BUILT = "self-built"
BUS_BUILT = "bus-built"
Counts = dict[str, dict[str, int]]
Sites = dict[tuple[str, str], list[str]]


def package_of(rel: str) -> str:
    """The ratchet's unit: the first two path components, `base/agents`, `gateway/routers`."""
    parts = rel.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def kind_of(name: str) -> str:
    return SELF_BUILT if name.endswith(".from_settings") else SHIM


def scan(repo_root: Path) -> Sites:
    """`(package, kind) -> ["path:line", ...]` for every ungoverned production module."""
    found: Sites = {}
    for rel in lint_common.tracked_files(repo_root):
        if not rel.endswith(".py") or not ambient_state.in_scope(rel):
            continue
        tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"), filename=rel)
        library = [
            (kind_of(hit.name), hit)
            for hit in (dbhandle.dials(tree) if dbhandle.package_of(rel) is None else [])
        ] + [
            (BUS_BUILT, hit)
            for hit in (busrule.builds(tree) if busrule.package_of(rel) is None else [])
        ]
        for kind, hit in library:
            found.setdefault((package_of(rel), kind), []).append(f"{rel}:{hit.line}")
    return found


def counts(sites: Sites) -> Counts:
    table: Counts = {}
    for (package, kind), where in sorted(sites.items()):
        table.setdefault(package, {})[kind] = len(where)
    return table


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, script-derived repo root
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False
    )


def base_counts(repo_root: Path) -> Counts | None:
    """The baseline at the base revision, `None` when it does not exist there yet."""
    merged = _git(repo_root, "merge-base", "HEAD", "origin/main")
    if merged.returncode != 0:
        return None
    shown = _git(repo_root, "show", f"{merged.stdout.strip()}:{BASELINE}")
    return json.loads(shown.stdout) if shown.returncode == 0 else None


def errors(sites: Sites, frozen: Counts, base: Counts | None) -> list[str]:
    out: list[str] = []
    current = counts(sites)
    for package in sorted({*current, *frozen}):
        for kind in (SHIM, SELF_BUILT, BUS_BUILT):
            have = current.get(package, {}).get(kind, 0)
            cap = frozen.get(package, {}).get(kind, 0)
            if have > cap:
                out += [
                    f"{where}: `{package}` has {have} {kind} site(s), {cap} frozen — "
                    "take a `Database` from the composition root instead"
                    for where in sites[(package, kind)]
                ]
            elif have < cap:
                out.append(
                    f"{BASELINE}:1: `{package}` is down to {have} {kind} site(s) from {cap} frozen "
                    "— lower the baseline (`handle_ratchet.py --write`)"
                )
            if base is not None and cap > base.get(package, {}).get(kind, 0):
                out.append(
                    f"{BASELINE}:1: `{package}` {kind} count {cap} is above the base revision's "
                    f"{base.get(package, {}).get(kind, 0)} — the baseline only shrinks"
                )
    return out


def main(argv: list[str]) -> int:
    sites = scan(_REPO_ROOT)
    path = _REPO_ROOT / BASELINE
    if argv == ["--write"]:
        path.write_text(json.dumps(counts(sites), indent=2, sort_keys=True) + "\n")
        return 0
    frozen: Counts = json.loads(path.read_text(encoding="utf-8"))
    problems = errors(sites, frozen, base_counts(_REPO_ROOT))
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
