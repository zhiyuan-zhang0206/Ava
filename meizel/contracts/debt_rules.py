"""`debt diff` for a change that edits the rules: the growth the rules cause, apart from the growth the content causes.

A rule change makes the checker find debt that was always in the code, so the recorded total grows
by design. That growth must not hide a content change that grows it too. Two measures:

- Rule growth: what this checker finds in the BASE tree's content, minus what the base records.
- Content growth: what the change records, minus what this checker finds in the base tree's content.

"What this checker finds in the base tree" is the baseline it would write for the base: this checker
(the code being judged) runs on a throwaway checkout of the base commit, declaring the legal edges and
recording the rest, exactly as a rule change's own baseline commit does. The base's recorded debt is
the old rule's result, since the base is green. Rule growth is reported and never judged. Content
growth is judged: every key the change records must be one the regenerated base records, with no
greater count, so a declaration cannot hide new violations and a violation cannot ride along. A file
moved in the same change shows up as a new key and fails: move files in their own change.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.debt import total
from meizel.contracts.debt_edit import prune as prune_debt
from meizel.contracts.debt_edit import record
from meizel.contracts.debt_report import Baselines, read_baselines
from meizel.contracts.declare import declare
from meizel.contracts.sync import prune
from meizel.graph.model import Code, MeizelError
from meizel.graph.universe import load_universe

__all__ = ["RuleDiff", "rule_diff"]

# A manifest or policy this checker cannot read says nothing about the base: counting it as no debt
# would let any rule change that breaks the format pass as clean.
_UNREADABLE = frozenset(
    {
        Code.MANIFEST_SYNTAX,
        Code.MANIFEST_KEY,
        Code.MANIFEST_CHILD,
        Code.MANIFEST_DUPLICATE,
        Code.MANIFEST_DEBT,
        Code.MANIFEST_ROLES,
        Code.POLICY_SYNTAX,
    }
)
_DEBT_ROWS = 5


class RuleDiff:
    """The two measures for one change, per code, and the keys that make the content measure fail."""

    def __init__(
        self, base: Counter[str], regenerated: Baselines, tree: Baselines, ref: str
    ) -> None:
        self.ref = ref
        self.base = base
        self.found = _totals(regenerated)
        self.tree = _totals(tree)
        self.excess = _excess(tree, regenerated)

    @property
    def content_grew(self) -> bool:
        return bool(self.excess)

    def lines(self) -> list[str]:
        codes = sorted(set(self.base) | set(self.found) | set(self.tree))
        out = [
            f"{'code':<20}{self.ref[:12]:>14}{'new rules':>11}{'tree':>8}{'rules':>8}{'content':>9}"
        ]
        for code in codes:
            out.append(
                f"{code:<20}{self.base[code]:>14}{self.found[code]:>11}{self.tree[code]:>8}"
                f"{self.found[code] - self.base[code]:>+8}{self.tree[code] - self.found[code]:>+9}"
            )
        rules = sum(self.found.values()) - sum(self.base.values())
        content = sum(self.tree.values()) - sum(self.found.values())
        out.append(
            f"{'total':<20}{sum(self.base.values()):>14}{sum(self.found.values()):>11}"
            f"{sum(self.tree.values()):>8}{rules:>+8}{content:>+9}"
        )
        return out

    def explanation(self) -> list[str]:
        if not self.excess:
            return [f"meizel: no content growth against {self.ref}: the rules account for the rest"]
        shown = self.excess[:_DEBT_ROWS]
        more = len(self.excess) - len(shown)
        return [
            f"meizel: the change records debt the new rules do not find in {self.ref[:12]}'s content:",
            *(
                f"  {where}: {key} ({code}) {have} recorded, {want} found"
                for where, code, key, have, want in shown
            ),
            *([f"  ... and {more} more"] if more else []),
            "meizel: a rule change records only what the rules find in the unchanged code; "
            "move files and change code in their own change",
        ]


def _totals(baselines: Baselines) -> Counter[str]:
    counts: Counter[str] = Counter()
    for table in baselines.values():
        for code, rows in table.items():
            counts[code.value] += total({code: rows})
    return counts


def _excess(tree: Baselines, found: Baselines) -> list[tuple[str, str, str, int, int]]:
    """Every (directory, code, key) the tree records beyond what the regenerated base records."""
    out: list[tuple[str, str, str, int, int]] = []
    for owner, table in sorted(tree.items()):
        for code, rows in table.items():
            for key, have in sorted(rows.items()):
                want = found.get(owner, {}).get(code, {}).get(key, 0)
                if have > want:
                    out.append((owner or ".", code.value, key, have, want))
    return out


@contextmanager
def _checkout(root: Path, ref: str) -> Generator[Path]:
    """A throwaway tracked checkout of `ref`, apart from the repository being judged."""
    if ref.startswith("-"):
        raise MeizelError(f"{ref}: not a git ref")
    parent = Path(tempfile.mkdtemp(prefix="meizel-base-"))
    target = parent / "base"
    try:
        _git(root, "worktree", "add", "--detach", str(target), ref)
        try:
            yield target
        finally:
            _git(root, "worktree", "remove", "--force", str(target))
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(root), *args], capture_output=True, check=False
    )
    if result.returncode != 0:
        raise MeizelError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")


def _stage(root: Path) -> None:
    _git(root, "add", "-A")


def _refuse_unreadable(analysis: Analysis, ref: str) -> None:
    unreadable = [d for d in analysis.diagnostics if d.code in _UNREADABLE]
    if unreadable:
        shown = "\n".join(d.render() for d in unreadable[:5])
        raise MeizelError(
            f"this checker cannot read the manifests of {ref}, so it cannot say what the new rules "
            f"find there:\n{shown}"
        )


def regenerated(base_root: Path, ref: str, jobs: int | None) -> Baselines:
    """The baseline this checker would write for the checkout at `base_root`."""
    _refuse_unreadable(analyze(base_root, jobs=jobs), ref)
    prune(base_root, jobs)
    prune_debt(base_root, jobs)
    declare(base_root, [""], jobs)
    _stage(base_root)  # the checker reads tracked files only, and these commands create manifests
    record(base_root, [""], jobs)
    _stage(base_root)
    record(base_root, [""], jobs)
    _stage(base_root)
    prune_debt(base_root, jobs)
    return read_baselines(load_universe(base_root))


def rule_diff(root: Path, ref: str, base: Counter[str], jobs: int | None = None) -> RuleDiff:
    """Compare the tree at `root` with `ref`, given the per-code debt the base records (`base`)."""
    with _checkout(root, ref) as checkout:
        found = regenerated(checkout, ref, jobs)
    return RuleDiff(base, found, read_baselines(load_universe(root)), ref)
