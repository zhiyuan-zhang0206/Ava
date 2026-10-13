"""Locality rules for the structure gate: single decision owners, and the shared site machinery.

The rules measure `path::target -> [line, ...]` sites per module. Single decision owners (Rule 5),
path imports (Rule 6) and ambient state (Rule 9) are documented in scripts/lint/code_structure.py.
Reaching another package's private names is no longer measured here: meizel reports it as
`private-name` and `private-module`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import cast

from meizel.language import path_imports
from meizel.owners import decisions
from meizel.state import ambient_state
from scripts.structure import baseline_shards

SECTIONS = ("owner_bypasses",)
STRICT_SECTIONS = ("owner_bypasses", path_imports.SECTION)
# No externally measured rule retains baseline allowances.
EXTERNAL_SECTIONS: dict[str, str] = {}
# White-box tests reach into privates by design; only test *directories* are
# exempt, since a governed module may legitimately be named test_*.py.
_TEST_DIR = re.compile(r"(^|/)tests?/")
Sites = dict[str, list[int]]


def measure(tree: ast.Module, rel_path: str, roots: tuple[str, ...]) -> dict[str, Sites]:
    if _TEST_DIR.search(rel_path):
        return {kind: {} for kind in SECTIONS if kind not in EXTERNAL_SECTIONS}
    return {"owner_bypasses": decisions.owner_bypasses(tree, rel_path, roots)}


def _new_site_message(kind: str, target: str) -> str:
    if kind == path_imports.SECTION:
        return f"imports by file path (`{target}`) — {path_imports.FIX}"
    if kind == ambient_state.SECTION:
        return ambient_state.site_message(target)
    return f"bypasses the single owner of `{target}` — {decisions.DECISIONS[target].fix}"


def site_errors(
    measured: dict[str, Sites],
    baseline: dict[str, dict[str, int]],
    *,
    scanned: set[str],
    repo_root: Path,
    renames: dict[str, str] | None = None,
) -> list[str]:
    """Reject every locality/path-import site; guard remaining ambient-state debt."""
    sources = {new: old for old, new in (renames or {}).items()}
    errors: list[str] = []
    for kind, sites in measured.items():
        if kind in EXTERNAL_SECTIONS:
            continue
        if kind in STRICT_SECTIONS:
            errors.extend(_growth_errors(kind, sites, {}, {}))
            continue
        errors.extend(_growth_errors(kind, sites, baseline[kind], sources))
        errors.extend(_stale_errors(kind, sites, baseline[kind], scanned, repo_root))
    return errors


def _growth_errors(
    kind: str, sites: Sites, frozen: dict[str, int], sources: dict[str, str]
) -> list[str]:
    errors: list[str] = []
    for key, lines in sorted(sites.items()):
        if len(lines) <= frozen.get(key, 0):
            continue
        path, _, target = key.partition("::")
        detail = _new_site_message(kind, target)
        if key in frozen:
            detail += f" (grew above its frozen count {frozen[key]})"
        elif path in sources:
            detail += f" (renamed file: migrate the baseline key from {sources[path]})"
        errors.extend(f"{path}:{line}: {detail}" for line in sorted(lines))
    return errors


def _stale_errors(
    kind: str, sites: Sites, frozen: dict[str, int], scanned: set[str], repo_root: Path
) -> list[str]:
    """Entries of scanned (or deleted) files whose sites shrank below the frozen count."""
    errors: list[str] = []
    for key, count in sorted(frozen.items()):
        path = key.partition("::")[0]
        if path not in scanned and (repo_root / path).exists():
            continue
        now = len(sites.get(key, []))
        if now < count:
            action = f"lower it to {now}" if now else "remove it"
            errors.append(
                f"{baseline_shards.shard_path(kind, key)}: stale {kind} entry {key} is frozen at {count} but the "
                f"code has {now} — {action} (the baseline must match reality)"
            )
    return errors


def _entry_scope(kind: str, scope: tuple[str, ...]) -> tuple[str, ...]:
    """Top-level directories an entry's path may start with: an externally measured section
    freezes test files, which sit in `tests/` or inside a package."""
    return (*scope, "tests", "scripts") if kind in EXTERNAL_SECTIONS else scope


def _valid_target(kind: str, target: str) -> bool:
    if kind == "owner_bypasses":
        return target in decisions.DECISIONS
    return ambient_state.is_target(target) if kind == ambient_state.SECTION else bool(target)


def validate_entries(kind: str, entries: object, scope: tuple[str, ...]) -> None:
    if not isinstance(entries, dict):
        raise ValueError(f"'{kind}' must be an object")  # noqa: TRY004 — invalid JSON schema
    for key, count in cast("dict[str, object]", entries).items():
        path_text, separator, target = key.partition("::")
        path = Path(path_text)
        valid_path = (
            path.parts
            and not path.is_absolute()
            and path.as_posix() == path_text
            and ".." not in path.parts
            and path.parts[0] in _entry_scope(kind, scope)
            and path.suffix == ".py"
        )
        valid_target = _valid_target(kind, target)
        if (
            not valid_path
            or not separator
            or not valid_target
            or type(count) is not int
            or count < 1
        ):
            raise ValueError(
                f"invalid {kind} entry {key!r}: expected a scoped .py path::target and integer >= 1"
            )
