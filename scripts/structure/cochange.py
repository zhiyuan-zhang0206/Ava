"""Locality co-change index: per-commit package spread and cross-package
co-change pairs over a rolling window of first-parent history.

Run: `.venv/bin/python scripts/structure/cochange.py [--days N | --commits N]
[--repo PATH] [--min-support N] [--min-confidence F] [--json]`.

This is an optional inspection tool (`docs/conventions/engineering/tech-debt.md`, locality
class), not a pre-commit gate: it reports on debt that
accumulates across many commits with no single owner or door, which
`docs/conventions/engineering/lint-vs-sweeper.md`'s graduation test puts on the sweeper side —
detection needs a rolling window of history and the fix needs judgement, so
neither half of the lint test holds. It always exits 0 on a successful scan
(an index, not a wall) and reuses `scripts.structure.imports.package_of`
for Python package resolution, so "package" here means exactly what Rule 4
(package doors, `scripts/lint/code_structure.py`) means by it.

Metric A (spread): per commit, the number of distinct packages among its
tracked source files, reported as p50/p90 by conventional-commit type
(fix/feat/refactor/other) plus the share of `fix` commits with spread >= 3,
and the 10 widest `fix` commits.

Metric B (co-change): over commits with 2-40 source files (a bulk commit
above that is a mass rename or vendor drop, not a coupling signal), file
pairs whose packages differ and whose co-occurrence count and confidence
both clear a threshold — candidate evidence that the two sides share a
decision with no single owner. Declared cross-process contract boundaries
(`_CONTRACT_BOUNDARIES`, e.g. `gateway/schemas/` <-> `ui/web/`, carried by
codegen) are excluded from pairing, and so is any file the branch tip no
longer has (paths follow renames to their current name; a deleted file names
no owner to fix); see "Calibration snapshot" in
`future/infra/engineering/locality.md` (read there, not edited here).
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import itertools
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.structure import imports

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DAYS = 90
_DEFAULT_MIN_SUPPORT = 8
_DEFAULT_MIN_CONFIDENCE = 0.6
_PAIR_MIN_FILES = 2
_PAIR_MAX_FILES = 40
_WIDEST_FIX_LIMIT = 10

_SRC_EXTENSIONS = (".py", ".ts", ".tsx", ".sh", ".sql")
_TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__)/")
# One named constant for everything excluded as generated / snapshot / lock
# content, matched with fnmatch so an exact path and a glob share one list.
_GENERATED_EXCLUDE = (
    "scripts/structure/baseline/*.json",
    "ui/web/openapi.json",
    "ui/web/src/lib/*-generated.ts",
    "uv.lock",
    "package-lock.json",
    "db/schema.sql",
)
# Declared cross-process contract boundaries: a pair of path prefixes whose
# co-change is carried by codegen, not a leaked decision. See the module
# docstring and future/infra/engineering/locality.md's "Calibration snapshot".
# Declared cross-process contracts: both sides change together by design, so a
# co-change there is a protocol change, not a leaked decision.
_CONTRACT_BOUNDARIES: tuple[tuple[str, str], ...] = (
    # REST wire shape, carried to the frontend by codegen.
    ("gateway/schemas/", "ui/web/"),
    # The Inspector response models live in gateway/inspect/schemas.py (its own
    # functional package, not gateway/schemas/), but they feed the same
    # OpenAPI-codegen contract with the frontend.
    ("gateway/inspect/schemas.py", "ui/web/"),
    # The exec child's request/result wire: `exec/protocol.py` owns it; the
    # child process (`exec_child`) and the parent's exec node package
    # (`agent/graph/exec/`) are its two ends.
    ("agent/execution/child.py", "agent/graph/exec/"),
)
_COMMIT_TYPES = ("fix", "feat", "refactor")
_TAG_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")
_CONVENTIONAL_RE = re.compile(r"^([a-z]+)(\([^)]*\))?!?:")
_RENAME_BRACE_RE = re.compile(r"\{([^{}]*) => ([^{}]*)\}")


def _is_test(rel_path: str) -> bool:
    if _TEST_DIR_RE.search(rel_path):
        return True
    name = rel_path.rsplit("/", 1)[-1]
    return name.startswith("test_") or ".test." in name


def _is_generated(rel_path: str) -> bool:
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in _GENERATED_EXCLUDE)


def _is_src(rel_path: str) -> bool:
    """Tracked source code: the extension whitelist, minus tests/docs/generated.

    `.md` docs are already excluded by the extension whitelist not listing
    `.md`; the check stays explicit in the module docstring for readers.
    """
    return (
        rel_path.endswith(_SRC_EXTENSIONS)
        and not _is_test(rel_path)
        and not _is_generated(rel_path)
    )


def _package_of_file(rel_path: str) -> str:
    """The package (a `/`-joined path key) owning a source file.

    Python files reuse the shared package anchor (`imports.package_of`), so
    "package" agrees with the lint exactly. Everything else uses its
    containing directory, except `ui/web/src/...`, which keeps one extra
    level of granularity (`ui/web/src/<first dir>`) instead of collapsing to
    the whole frontend source tree.
    """
    if rel_path.endswith(".py"):
        return "/".join(imports.package_of(rel_path))
    parts = rel_path.split("/")
    if parts[:3] == ["ui", "web", "src"] and len(parts) > 4:
        return "/".join(parts[:4])
    return "/".join(parts[:-1])


def _is_contract_pair(a: str, b: str) -> bool:
    for prefix_a, prefix_b in _CONTRACT_BOUNDARIES:
        if (a.startswith(prefix_a) and b.startswith(prefix_b)) or (
            a.startswith(prefix_b) and b.startswith(prefix_a)
        ):
            return True
    return False


def _commit_type(subject: str) -> str:
    stripped = _TAG_PREFIX_RE.sub("", subject)
    match = _CONVENTIONAL_RE.match(stripped)
    kind = match.group(1) if match else None
    return kind if kind in _COMMIT_TYPES else "other"


def _numstat_paths(raw: str) -> tuple[str | None, str]:
    """`(old, new)` from one numstat path field (`git log -M --numstat`); `old`
    is None unless the line is a rename.

    A rename appears either as a full `old => new`, or compacted to a common
    prefix as `dir/{old => new}/file`.
    """
    if " => " not in raw:
        return None, raw
    if _RENAME_BRACE_RE.search(raw):
        old = _RENAME_BRACE_RE.sub(lambda m: m.group(1), raw).replace("//", "/")
        new = _RENAME_BRACE_RE.sub(lambda m: m.group(2), raw).replace("//", "/")
        return old, new
    old, new = raw.split(" => ", 1)
    return old, new


@dataclass(frozen=True)
class Commit:
    sha: str
    date: str
    subject: str
    type: str
    files: list[str]  # deduped, sorted, src-only, named as at the window's newest commit


_PathChange = tuple[str | None, str]  # (pre-rename path or None, path in this commit)


def _raw_commit_blocks(text: str) -> list[tuple[str, str, str, list[_PathChange]]]:
    """Group numstat output lines under their preceding `@@sha\\tdate\\tsubject`."""
    blocks: list[tuple[str, str, str, list[_PathChange]]] = []
    header: tuple[str, str, str] | None = None
    paths: list[_PathChange] = []
    for line in text.splitlines():
        if line.startswith("@@"):
            if header is not None:
                blocks.append((*header, paths))
            sha, when, subject = line[2:].split("\t", 2)
            header = (sha, when, subject)
            paths = []
        elif line.strip() and header is not None:
            _added, _deleted, raw_path = line.split("\t", 2)
            paths.append(_numstat_paths(raw_path))
    if header is not None:
        blocks.append((*header, paths))
    return blocks


def _commits_from_log(text: str) -> list[Commit]:
    """Commits with every path resolved to its name at the window's newest commit.

    `git log` lists newest first, so a rename is met before the older commits that
    still use the old name: each rename records `old -> current`, and older paths
    resolve through it. Co-change then follows a file across renames instead of
    splitting its history under two names.
    """
    current: dict[str, str] = {}
    commits: list[Commit] = []
    for sha, when, subject, changes in _raw_commit_blocks(text):
        files: set[str] = set()
        for old, path in changes:
            resolved = current.get(path, path)
            files.add(resolved)
            if old is not None:
                current[old] = resolved
        commits.append(
            Commit(
                sha=sha,
                date=when,
                subject=subject,
                type=_commit_type(subject),
                files=sorted(f for f in files if _is_src(f)),
            )
        )
    return commits


def _run_git_log(repo: Path, branch: str, *, days: int | None, commits: int | None) -> str:
    cmd = [
        "git",
        "-C",
        str(repo),
        "log",
        "--first-parent",
        "--no-merges",
        "-M",
        "--numstat",
        "--date=short",
        "--format=@@%H\t%ad\t%s",
    ]
    if commits is not None:
        cmd.append(f"-n{commits}")
    elif days is not None:
        cmd.append(f"--since={days}.days.ago")
    cmd.append(branch)
    result = subprocess.run(  # noqa: S603 - fixed argv, caller-controlled repo/branch only
        cmd, capture_output=True, text=True, check=True, timeout=120
    )
    return result.stdout


def _tip_files(repo: Path, branch: str) -> frozenset[str]:
    result = subprocess.run(  # noqa: S603 - fixed argv, caller-controlled repo/branch only
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", branch],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return frozenset(result.stdout.splitlines())


def _head_sha(repo: Path, branch: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, caller-controlled repo/branch only
        ["git", "-C", str(repo), "rev-parse", "--short", branch],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _distinct_packages(files: list[str]) -> set[str]:
    return {_package_of_file(f) for f in files}


@dataclass(frozen=True)
class SpreadStats:
    n: int
    p50: int
    p90: int


def _spread_by_type(commits: list[Commit]) -> dict[str, SpreadStats]:
    by_type: dict[str, list[int]] = defaultdict(list)
    for commit in commits:
        by_type[commit.type].append(len(_distinct_packages(commit.files)))
    return {
        kind: SpreadStats(n=len(values), p50=_percentile(values, 0.5), p90=_percentile(values, 0.9))
        for kind, values in by_type.items()
    }


def _fix_wide_share(commits: list[Commit]) -> tuple[int, int, float]:
    fixes = [c for c in commits if c.type == "fix"]
    wide = [c for c in fixes if len(_distinct_packages(c.files)) >= 3]
    share = len(wide) / len(fixes) if fixes else 0.0
    return len(wide), len(fixes), share


@dataclass(frozen=True)
class WideCommit:
    sha: str
    subject: str
    spread: int
    packages: list[str]


def _widest_fix_commits(
    commits: list[Commit], *, limit: int = _WIDEST_FIX_LIMIT
) -> list[WideCommit]:
    fixes = [c for c in commits if c.type == "fix"]
    ranked = sorted(fixes, key=lambda c: (-len(_distinct_packages(c.files)), c.sha))
    return [
        WideCommit(
            sha=c.sha,
            subject=c.subject,
            spread=len(_distinct_packages(c.files)),
            packages=sorted(_distinct_packages(c.files)),
        )
        for c in ranked[:limit]
    ]


def _paired_commits(commits: list[Commit]) -> list[Commit]:
    return [c for c in commits if _PAIR_MIN_FILES <= len(c.files) <= _PAIR_MAX_FILES]


def _cochange_counts(commits: list[Commit]) -> tuple[Counter[tuple[str, str]], Counter[str]]:
    """Cross-package co-occurrence per file pair, and per-file touch count.

    Both counters are scoped to the same commit population (2-40 src files)
    so confidence (`c / min(n(a), n(b))`) compares like with like: `n(x)`
    never counts a commit that could not contribute to any pair.
    """
    pair_counts: Counter[tuple[str, str]] = Counter()
    touch_counts: Counter[str] = Counter()
    for commit in commits:
        touch_counts.update(commit.files)
        for a, b in itertools.combinations(commit.files, 2):
            if _package_of_file(a) == _package_of_file(b) or _is_contract_pair(a, b):
                continue
            pair_counts[(a, b)] += 1
    return pair_counts, touch_counts


@dataclass(frozen=True)
class PairRow:
    a: str
    b: str
    c: int
    n_a: int
    n_b: int
    confidence: float


def _strong_pairs(
    pair_counts: Counter[tuple[str, str]],
    touch_counts: Counter[str],
    *,
    tip_files: frozenset[str],
    min_support: int,
    min_confidence: float,
) -> list[PairRow]:
    """Pairs clearing both thresholds whose files both still exist at the tip.

    A deleted file names no owner to move a decision into — its record is
    history, not a finding (renames are already folded into the current name
    by `_commits_from_log`).
    """
    rows: list[PairRow] = []
    for (a, b), c in pair_counts.items():
        if a not in tip_files or b not in tip_files:
            continue
        n_a, n_b = touch_counts[a], touch_counts[b]
        confidence = c / min(n_a, n_b)
        if c >= min_support and confidence >= min_confidence:
            rows.append(PairRow(a=a, b=b, c=c, n_a=n_a, n_b=n_b, confidence=confidence))
    rows.sort(key=lambda r: (-r.c, -r.confidence, r.a, r.b))
    return rows


@dataclass(frozen=True)
class PackagePairRow:
    a: str
    b: str
    count: int


def _package_pairs(rows: list[PairRow]) -> list[PackagePairRow]:
    counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        pa, pb = sorted((_package_of_file(row.a), _package_of_file(row.b)))
        counts[pa, pb] += 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [PackagePairRow(a=pa, b=pb, count=n) for (pa, pb), n in ranked]


@dataclass(frozen=True)
class Report:
    repo: str
    branch: str
    window: str
    head: str
    min_support: int
    min_confidence: float
    commit_count: int
    spread_by_type: dict[str, SpreadStats]
    fix_wide_count: int
    fix_count: int
    fix_wide_share: float
    widest_fix: list[WideCommit]
    paired_commit_count: int
    strong_pairs: list[PairRow]
    package_pairs: list[PackagePairRow]


def _build_report(
    commits: list[Commit],
    *,
    repo: str,
    branch: str,
    window: str,
    head: str,
    tip_files: frozenset[str],
    min_support: int,
    min_confidence: float,
) -> Report:
    wide_count, fix_count, share = _fix_wide_share(commits)
    paired = _paired_commits(commits)
    pair_counts, touch_counts = _cochange_counts(paired)
    strong = _strong_pairs(
        pair_counts,
        touch_counts,
        tip_files=tip_files,
        min_support=min_support,
        min_confidence=min_confidence,
    )
    return Report(
        repo=repo,
        branch=branch,
        window=window,
        head=head,
        min_support=min_support,
        min_confidence=min_confidence,
        commit_count=len(commits),
        spread_by_type=_spread_by_type(commits),
        fix_wide_count=wide_count,
        fix_count=fix_count,
        fix_wide_share=share,
        widest_fix=_widest_fix_commits(commits),
        paired_commit_count=len(paired),
        strong_pairs=strong,
        package_pairs=_package_pairs(strong),
    )


def _render_markdown(report: Report) -> str:
    lines = [
        "# Locality co-change report",
        "",
        f"- repo: {report.repo}",
        f"- branch: {report.branch}",
        f"- window: {report.window}",
        f"- HEAD: {report.head}",
        f"- thresholds: min-support={report.min_support}, min-confidence={report.min_confidence}",
        f"- commits scanned: {report.commit_count}",
        "",
        "## Metric A -- spread per commit",
        "",
        "| type | n | spread p50 | spread p90 |",
        "|---|---|---|---|",
    ]
    for kind in (*_COMMIT_TYPES, "other"):
        stats = report.spread_by_type.get(kind)
        if stats is not None:
            lines.append(f"| {kind} | {stats.n} | {stats.p50} | {stats.p90} |")
    lines += [
        "",
        f"- fix commits with spread >= 3: {report.fix_wide_share:.0%} "
        f"({report.fix_wide_count}/{report.fix_count})",
        "",
        "### Widest fix commits",
        "",
    ]
    if report.widest_fix:
        for wide in report.widest_fix:
            lines.append(f"- `{wide.sha[:10]}` spread={wide.spread} -- {wide.subject}")
            lines.append(f"  packages: {', '.join(wide.packages)}")
    else:
        lines.append("(no fix commits in window)")
    lines += [
        "",
        "## Metric B -- cross-package co-change pairs",
        "",
        f"- qualifying commits ({_PAIR_MIN_FILES}-{_PAIR_MAX_FILES} src files): "
        f"{report.paired_commit_count}",
        f"- strong pairs: {len(report.strong_pairs)}",
        "",
        "| a | b | c | n(a) | n(b) | confidence |",
        "|---|---|---|---|---|---|",
    ]
    for pair in report.strong_pairs:
        lines.append(
            f"| {pair.a} | {pair.b} | {pair.c} | {pair.n_a} | {pair.n_b} | {pair.confidence:.0%} |"
        )
    lines += [
        "",
        "### Package pairs by strong-file-pair count",
        "",
        "| package a | package b | strong file pairs |",
        "|---|---|---|",
    ]
    for pair in report.package_pairs:
        lines.append(f"| {pair.a} | {pair.b} | {pair.count} |")
    return "\n".join(lines) + "\n"


def _window_description(*, branch: str, days: int | None, commits: int | None) -> str:
    if commits is not None:
        return f"last {commits} first-parent commits on {branch}"
    if days is None:
        raise AssertionError("main() must resolve one of --days/--commits before rendering")
    since = datetime.now(UTC).date() - timedelta(days=days)
    return f"last {days} days (since {since.isoformat()}) on {branch}, first-parent"


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(_REPO_ROOT), help="repo root (default: this repo)")
    # origin/main, not the local `main`: this repo works in worktrees off a fetched
    # origin, and the shared local `main` ref is routinely far behind it.
    parser.add_argument("--branch", default="origin/main")
    window = parser.add_mutually_exclusive_group()
    window.add_argument(
        "--days", type=int, help=f"rolling window in days (default {_DEFAULT_DAYS})"
    )
    window.add_argument("--commits", type=int, help="rolling window as a commit count")
    parser.add_argument("--min-support", type=int, default=_DEFAULT_MIN_SUPPORT)
    parser.add_argument("--min-confidence", type=float, default=_DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if args.days is not None:
        days = args.days
    else:
        days = None if args.commits is not None else _DEFAULT_DAYS
    window_desc = _window_description(branch=args.branch, days=days, commits=args.commits)

    try:
        log_text = _run_git_log(repo, args.branch, days=days, commits=args.commits)
        head = _head_sha(repo, args.branch)
        tip_files = _tip_files(repo, args.branch)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        print(f"error: git log failed for {repo} ({args.branch}): {stderr}", file=sys.stderr)
        return 1

    report = _build_report(
        _commits_from_log(log_text),
        repo=str(repo),
        branch=args.branch,
        window=window_desc,
        head=head,
        tip_files=tip_files,
        min_support=args.min_support,
        min_confidence=args.min_confidence,
    )
    if args.as_json:
        print(json.dumps(dataclasses.asdict(report), indent=2, sort_keys=True))
    else:
        print(_render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
