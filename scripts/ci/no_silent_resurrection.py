#!/usr/bin/env python3
"""Fail a PR that silently resurrects lines main deleted recently.

Decision record: decisions/2026-10-04-no-silent-resurrection.md.
Working convention: conventions/no-silent-resurrection.md.

The incident: #4245 (6ecfe14c3) deleted the delivery-watchdog field code; #4207
(151bc92fe) then landed after replaying a stale branch through a conflict
resolution and carried the old content of four of those files back. The
resurrected code compiled and its tests passed, so every existing gate stayed
green. This check compares the lines a PR adds with the lines main deleted in
the last N days (default 30) and fails on a match unless a commit in the PR
carries a ``Resurrects: <reason>`` line (optionally naming the deleting sha or
a path) or reverts the deleting commit (``This reverts commit <sha>``).

Pipeline (one function per stage):

1. ``added_runs`` -- ``git diff -U0 -M <merge-base> <head>``, split into runs of
   consecutive added lines (any non-added line ends a run).
2. ``candidate_texts`` -- normalized (leading/trailing whitespace stripped)
   added lines that are "strong": not blank, not a comment/import/pure
   punctuation, MIN_STRONG_LENGTH..MAX_STRONG_LENGTH chars (a longer line is
   a data blob: it never becomes a candidate), >= 2 identifiers of >= 3 chars.
   Weak lines never fail the check; they only bridge runs.
3. ``alive_candidates`` -- candidate texts still present in the base tree
   (skipped paths excluded) are not dead: a move cannot hide a resurrection,
   and a line that still exists is not resurrected.
4. ``deleted_candidates`` -- one streaming ``git log --since=<N>days
   --no-merges -p -U0 <base>`` pass. A commit "deletes" a candidate line when
   its diff removes it from a non-skipped path and the commit does not re-add
   the text somewhere in the same commit (a move is not a deletion).
5. ``find_hits`` -- inside a run, a stretch where every strong line is dead
   (weak, blank and still-present lines bridge; a strong line that is neither
   breaks the stretch) is a hit when it holds >= MIN_DEAD_LINES dead strong lines or one
   distinctive dead line (an identifier of >= DISTINCTIVE_IDENTIFIER_LENGTH
   chars with an underscore or camelCase). Each hit is attributed to the commit
   covering the most of its dead lines (ties: the most recent).
6. ``collect_allowances`` / ``apply_allowances`` -- commit messages in
   merge-base..head; ``Resurrects:`` lines allow every hit, or only the hits
   they name by deleting sha or path; ``This reverts commit <sha>`` allows the
   hits that sha deleted. A declaration matching nothing is warned about.

Exit codes: 0 no blocking hit (clean, or every hit allowed), 1 blocking hits,
2 usage or infrastructure (git) failure. Output is English, on stdout;
warnings go to stderr; the failing message carries the allowance recipe.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE = "origin/main"
DEFAULT_HEAD = "HEAD"
DEFAULT_DAYS = 30

# A "strong" line is meaningful enough that a resurrected run of them (or one
# carrying a distinctive identifier) should have been noticed by the author.
# The thresholds are the tuning knobs, set from the 30-day false-positive
# sweep described in conventions/no-silent-resurrection.md: the minimum unit
# is three lines (the incident's blocks were 3+ lines; two-line idiom matches
# dominated the noise) and a solo line needs a >=20-char identifier (short
# framework names like OperationalError matched everything).
MIN_STRONG_LENGTH = 14
MAX_STRONG_LENGTH = 1000
MIN_IDENTIFIERS = 2
MIN_IDENTIFIER_LENGTH = 3
DISTINCTIVE_IDENTIFIER_LENGTH = 20
MIN_DEAD_LINES = 3
SAMPLE_LINES = 3

# Lock and generated files carry line churn that no reader reviews: they are
# regenerated from their source, which the check does see. Both the current
# location and the pre-2026-09-29 layout (the `shared -> base` move) are listed
# so the 30-day window stays clean across the move. Mirrors the codegen
# freshness hooks in .pre-commit-config.yaml.
SKIPPED_FILES = frozenset(
    {
        "uv.lock",
        "ui/web/package-lock.json",
        "ui/web/openapi.json",
        "ui/web/src/lib/types-generated.ts",
        "base/host/env/config_lite_table.json",
        "shared/config_lite_table.json",
        "base/events/registry.md",
        "shared/events/registry.md",
        "db/schema.sql",
        "scripts/zombie_pyright_ignores.registry",
        "base/agents/api.txt",
        "shared/agents/api.txt",
        "base/events/api.txt",
        "shared/events/api.txt",
        "scripts/structure/baseline.json",
    }
)
SKIPPED_DIRS = ("migrations/", "scripts/structure/baseline/")

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_CASE = re.compile(r"[a-z][A-Z]")
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "*/", "--", "<!--")
_IMPORT_PREFIXES = ("import ", "from ", "require(", "#include", "using ")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_RESURRECTS_LINE = re.compile(r"^\s*Resurrects:\s*(.+?)\s*$")
_REVERT_LINE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
_SHA_TOKEN = re.compile(r"[0-9a-f]{7,40}")
_EXTENSION = re.compile(r"[A-Za-z0-9]{2,8}")


class GitError(RuntimeError):
    """A git invocation the check depends on failed."""


def _git(*args: str, cwd: Path) -> str:
    """Run one git command with the check's fixed configuration."""
    completed = subprocess.run(  # noqa: S603
        ["git", "-c", "color.ui=false", "-c", "core.quotepath=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        message = detail[0] if detail else f"exit {completed.returncode}"
        raise GitError(f"`git {args[0]}` failed: {message}")
    return completed.stdout


@dataclass(frozen=True)
class Line:
    """One added line of the PR diff (normalized)."""

    number: int
    text: str
    strong: bool
    distinctive: bool


@dataclass(frozen=True)
class Run:
    """A maximal sequence of consecutive added lines inside one file."""

    path: str
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class Commit:
    """A main commit that deleted at least one candidate line."""

    sha: str
    date: str
    subject: str
    deleted_files: frozenset[str]


@dataclass(frozen=True)
class Hit:
    """A run stretch that resurrects lines one commit deleted."""

    path: str
    first_line: int
    last_line: int
    dead_count: int
    sample: tuple[str, ...]
    commit: Commit
    file_deleted_by_commit: bool


@dataclass(frozen=True)
class Allowance:
    """A ``Resurrects:`` line or revert declaration carried by the PR."""

    commit: str
    text: str
    shas: tuple[str, ...]
    paths: tuple[str, ...]
    allow_all: bool


def _skipped(path: str) -> bool:
    return path in SKIPPED_FILES or path.startswith(SKIPPED_DIRS)


def _has_distinctive_identifier(text: str) -> bool:
    for token in _IDENTIFIER.findall(text):
        if len(token) >= DISTINCTIVE_IDENTIFIER_LENGTH and (
            "_" in token or _CAMEL_CASE.search(token)
        ):
            return True
    return False


def _classify(text: str) -> tuple[bool, bool]:
    """Return (strong, distinctive) for one normalized added line."""
    if not MIN_STRONG_LENGTH <= len(text) <= MAX_STRONG_LENGTH:
        return False, False
    if text.startswith(_COMMENT_PREFIXES) or text.startswith(_IMPORT_PREFIXES):
        return False, False
    if not re.search(r"[A-Za-z0-9]", text):
        return False, False
    identifiers = [t for t in _IDENTIFIER.findall(text) if len(t) >= MIN_IDENTIFIER_LENGTH]
    strong = len(identifiers) >= MIN_IDENTIFIERS
    return strong, strong and _has_distinctive_identifier(text)


def _added_runs(merge_base: str, head: str, cwd: Path) -> list[Run]:
    """Parse the PR diff into runs of consecutive added lines."""
    diff = _git("diff", "--no-ext-diff", "-U0", "-M", merge_base, head, cwd=cwd)
    runs: list[Run] = []
    path: str | None = None
    run: list[Line] = []
    number = 0
    in_hunk = False

    def flush() -> None:
        nonlocal run
        if run and path is not None and not _skipped(path):
            runs.append(Run(path, tuple(run)))
        run = []

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            flush()
            in_hunk = False
            path = None
            continue
        if raw.startswith("@@ "):
            flush()
            in_hunk = True
            header = _HUNK_HEADER.match(raw)
            if header is None:
                raise ValueError(f"unparsable hunk header in diff output: {raw!r}")
            number = int(header.group(1))
            continue
        if not in_hunk:
            if raw.startswith("+++ b/"):
                path = raw[len("+++ b/") :]
            elif raw.startswith("+++ /dev/null"):
                path = None
            continue
        marker = raw[:1]
        if marker == "+":
            text = raw[1:].strip()
            if path is not None:
                strong, distinctive = _classify(text)
                run.append(Line(number, text, strong, distinctive))
            number += 1
        elif marker == " ":
            flush()
            number += 1
        elif marker == "-":
            flush()
    flush()
    return runs


def _candidate_texts(runs: list[Run]) -> set[str]:
    return {line.text for run in runs for line in run.lines if line.strong and line.text}


def _alive_candidates(candidates: set[str], base: str, cwd: Path) -> set[str]:
    """Candidate texts still present anywhere in the base tree (skipped paths excluded)."""
    if not candidates:
        return set()
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".patterns", delete=False
    ) as handle:
        handle.write("\n".join(sorted(candidates)) + "\n")
        pattern_path = handle.name
    try:
        completed = subprocess.run(  # noqa: S603
            [
                "git",
                "-c",
                "color.ui=false",
                "-c",
                "core.quotepath=false",
                "grep",
                "-h",
                "-I",
                "-F",
                "-f",
                pattern_path,
                base,
                "--",
                ".",
                *[f":(exclude){path}" for path in sorted(SKIPPED_FILES)],
                *[f":(exclude){directory}" for directory in SKIPPED_DIRS],
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        Path(pattern_path).unlink(missing_ok=True)
    if completed.returncode > 1:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise GitError(f"`git grep` over {base} failed: {detail[0] if detail else 'exit'}")
    return {line.strip() for line in completed.stdout.splitlines()} & candidates


class _LogScan:
    """One streaming pass over `git log -p`: candidate deletions per commit."""

    def __init__(self, candidates: set[str]) -> None:
        self.candidates = candidates
        self.dead: dict[str, list[Commit]] = defaultdict(list)
        self.scanned = 0
        self.sha = ""
        self.date = ""
        self.subject = ""
        self.deleted: set[str] = set()
        self.added: set[str] = set()
        self.deleted_files: set[str] = set()
        self.minus_path: str | None = None
        self.current_path: str | None = None
        self.in_hunk = False

    def feed(self, raw: str) -> None:
        line = raw.rstrip("\n")
        if line.startswith("\x01"):
            self._begin(line[1:])
        elif not self.sha:
            return
        elif line.startswith("diff --git "):
            self.in_hunk = False
        elif line.startswith("@@ "):
            self.in_hunk = True
        elif not self.in_hunk:
            self._feed_header(line)
        else:
            self._feed_hunk(line)

    def _begin(self, header: str) -> None:
        self.finish()
        self.sha, self.date, self.subject = header.split("\x01", 2)
        self.scanned += 1
        self.in_hunk = False
        self.minus_path = None
        self.current_path = None

    def finish(self) -> None:
        """Close the current commit: candidate deletions it did not re-add."""
        if self.sha and self.deleted - self.added:
            commit = Commit(self.sha, self.date, self.subject, frozenset(self.deleted_files))
            for text in self.deleted - self.added:
                self.dead[text].append(commit)
        self.deleted = set()
        self.added = set()
        self.deleted_files = set()

    def _feed_header(self, line: str) -> None:
        if line.startswith("--- a/"):
            self.minus_path = line[len("--- a/") :]
        elif line.startswith("+++ /dev/null"):
            if self.minus_path is not None:
                self.deleted_files.add(self.minus_path)
            self.current_path = self.minus_path
        elif line.startswith("+++ b/"):
            self.current_path = line[len("+++ b/") :]

    def _feed_hunk(self, line: str) -> None:
        marker = line[:1]
        if marker not in "+-":
            return
        text = line[1:].strip()
        if text not in self.candidates:
            return
        if marker == "+":
            self.added.add(text)
        elif self.current_path is not None and not _skipped(self.current_path):
            self.deleted.add(text)


def _deleted_candidates(
    base: str, days: int, candidates: set[str], cwd: Path
) -> tuple[dict[str, list[Commit]], int]:
    """Return (dead candidate text -> deleting commits, commits scanned).

    One streaming `git log --since=<days>days --no-merges -p -U0 <base>` pass:
    a commit "deletes" a candidate line when its diff removes it from a
    non-skipped path and the commit does not re-add the text in the same
    commit (a move is not a deletion).
    """
    if not candidates:
        return {}, 0
    stream = subprocess.Popen(  # noqa: S603
        [
            "git",
            "-c",
            "color.ui=false",
            "-c",
            "core.quotepath=false",
            "log",
            f"--since={days}.days",
            "--no-merges",
            "--format=%x01%H%x01%cs%x01%s",
            "-p",
            "-U0",
            base,
        ],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout = stream.stdout
    if stdout is None:
        raise GitError("git log produced no output stream")
    scan = _LogScan(candidates)
    for raw in stdout:
        scan.feed(raw)
    scan.finish()
    _, stderr = stream.communicate()
    if stream.returncode != 0:
        raise GitError(f"`git log --since={days}.days` over {base} failed: {stderr.strip()}")
    return scan.dead, scan.scanned


def _find_hits(runs: list[Run], alive: set[str], dead: dict[str, list[Commit]]) -> list[Hit]:
    hits: list[Hit] = []
    for run in runs:
        segment: list[Line] = []
        for line in run.lines:
            if line.strong and line.text not in alive and line.text not in dead:
                hits.extend(_segment_hits(run.path, segment, alive, dead))
                segment = []
                continue
            segment.append(line)
        hits.extend(_segment_hits(run.path, segment, alive, dead))
    return hits


def _segment_hits(
    path: str, segment: list[Line], alive: set[str], dead: dict[str, list[Commit]]
) -> list[Hit]:
    dead_lines = [
        line for line in segment if line.strong and line.text in dead and line.text not in alive
    ]
    if not dead_lines:
        return []
    if len(dead_lines) < MIN_DEAD_LINES and not any(line.distinctive for line in dead_lines):
        return []
    coverage: Counter[Commit] = Counter()
    for line in dead_lines:
        coverage.update(dead[line.text])
    best = max(coverage, key=lambda commit: (coverage[commit], commit.date, commit.sha))
    return [
        Hit(
            path=path,
            first_line=dead_lines[0].number,
            last_line=dead_lines[-1].number,
            dead_count=len(dead_lines),
            sample=tuple(line.text for line in dead_lines[:SAMPLE_LINES]),
            commit=best,
            file_deleted_by_commit=path in best.deleted_files,
        )
    ]


def _looks_like_file(token: str) -> bool:
    head, dot, extension = token.rpartition(".")
    if not dot or not head:
        return False
    return _EXTENSION.fullmatch(extension) is not None and any(char.isalpha() for char in extension)


def _declared_targets(reason: str) -> tuple[list[str], list[str]]:
    shas: list[str] = []
    paths: list[str] = []
    for raw_token in reason.split():
        token = raw_token.strip("[]()<>{}\"'`,;:")
        if not token:
            continue
        if "/" in token:
            paths.append(token)
        elif _SHA_TOKEN.fullmatch(token.lower()):
            if any(char.isdigit() for char in token):
                shas.append(token.lower())
        elif _looks_like_file(token):
            paths.append(token)
    return shas, paths


def _collect_allowances(merge_base: str, head: str, cwd: Path) -> list[Allowance]:
    """Read the PR's commit messages for `Resurrects:` and revert declarations."""
    log = _git("log", "--format=%x01%H%n%B", f"{merge_base}..{head}", cwd=cwd)
    allowances: list[Allowance] = []
    commit = ""
    for raw in log.splitlines():
        if raw.startswith("\x01"):
            commit = raw[1:].strip()
            continue
        match = _RESURRECTS_LINE.match(raw)
        if match:
            shas, paths = _declared_targets(match.group(1))
            allowances.append(
                Allowance(commit, raw.strip(), tuple(shas), tuple(paths), not shas and not paths)
            )
            continue
        revert = _REVERT_LINE.search(raw)
        if revert:
            allowances.append(
                Allowance(commit, raw.strip(), (revert.group(1),), (), allow_all=False)
            )
    return allowances


def _path_matches(declared: str, path: str) -> bool:
    declared = declared.rstrip("/")
    return bool(declared) and (path == declared or path.startswith(declared + "/"))


def _covers(allowance: Allowance, hit: Hit) -> bool:
    if allowance.allow_all:
        return True
    if any(hit.commit.sha.startswith(sha) for sha in allowance.shas):
        return True
    return any(_path_matches(declared, hit.path) for declared in allowance.paths)


def _apply_allowances(
    hits: list[Hit], allowances: list[Allowance]
) -> tuple[list[tuple[Hit, Allowance]], list[Hit], list[Allowance]]:
    used = {
        index
        for index, allowance in enumerate(allowances)
        if any(_covers(allowance, hit) for hit in hits)
    }
    allowed: list[tuple[Hit, Allowance]] = []
    blocked: list[Hit] = []
    for hit in hits:
        covering = next((a for a in allowances if _covers(a, hit)), None)
        if covering is None:
            blocked.append(hit)
        else:
            allowed.append((hit, covering))
    unused = [a for index, a in enumerate(allowances) if index not in used]
    return allowed, blocked, unused


def _span(hit: Hit) -> str:
    if hit.first_line == hit.last_line:
        return str(hit.first_line)
    return f"{hit.first_line}-{hit.last_line}"


def _warn_unused(unused: list[Allowance]) -> None:
    for allowance in unused:
        print(
            f"no-silent-resurrection: warning: declaration matched no hit: "
            f"{allowance.text!r} (in {allowance.commit[:9]})",
            file=sys.stderr,
        )


def _line_recipe(hit: Hit) -> None:
    print()
    print(
        "no-silent-resurrection: an intentional resurrection is allowed by a "
        "`Resurrects: <reason>` line"
    )
    print("in any commit message of this PR; name the deleting sha or a path to allow only")
    print("matching hits:")
    print(f"    Resurrects: {hit.commit.sha[:9]}    # this hit's deleting commit")
    print(f"    Resurrects: {hit.path}    # this hit's file (or a directory)")
    print("A `Resurrects:` line with neither a sha nor a path allows every hit. A commit that")
    print("reverts the deleting commit (`This reverts commit <sha>`) is allowed automatically.")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="no_silent_resurrection",
        description="Fail when the PR's added lines match lines main deleted recently.",
    )
    parser.add_argument(
        "--base", default=DEFAULT_BASE, help="base branch ref (default: origin/main)"
    )
    parser.add_argument("--head", default=DEFAULT_HEAD, help="head ref (default: HEAD)")
    parser.add_argument(
        "--days",
        type=_positive_days,
        default=DEFAULT_DAYS,
        help="history window in days (default: 30)",
    )
    return parser.parse_args(argv)


def _positive_days(value: str) -> int:
    days = int(value)
    if days < 1:
        raise argparse.ArgumentTypeError("must be a positive number of days")
    return days


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cwd = Path(_git("rev-parse", "--show-toplevel", cwd=Path.cwd()).strip())
        merge_base = _merge_base(args.base, args.head, cwd)
        runs = _added_runs(merge_base, args.head, cwd)
        candidates = _candidate_texts(runs)
        alive = _alive_candidates(candidates, args.base, cwd)
        dead, scanned = _deleted_candidates(args.base, args.days, candidates, cwd)
        hits = _find_hits(runs, alive, dead)
        allowances = _collect_allowances(merge_base, args.head, cwd)
    except (GitError, OSError) as error:
        print(f"no-silent-resurrection: error: {error}", file=sys.stderr)
        return 2

    added = sum(len(run.lines) for run in runs)
    files = len({run.path for run in runs})
    print(
        f"no-silent-resurrection: {added} added line(s) in {files} file(s); merge-base "
        f"{merge_base[:9]} of {args.base}..{args.head}"
    )
    print(
        f"no-silent-resurrection: {len(candidates)} candidate line(s); {len(alive)} still in "
        f"the base tree; {len(dead)} match lines deleted in {scanned} commit(s) over the "
        f"last {args.days} day(s)"
    )
    if not hits:
        print("no-silent-resurrection: no resurrection detected.")
        _warn_unused(allowances)
        return 0

    allowed, blocked, unused = _apply_allowances(hits, allowances)
    for hit, allowance in allowed:
        print(
            f"no-silent-resurrection: ALLOWED {hit.path}:{_span(hit)} - covered by "
            f"{allowance.text!r} in {allowance.commit[:9]}"
        )
    for hit in blocked:
        print(
            f"no-silent-resurrection: HIT {hit.path}:{_span(hit)} ({hit.dead_count} dead line(s))"
        )
        print(
            f'    deleted by {hit.commit.sha[:9]} "{hit.commit.subject}" ({hit.commit.date})'
            + ("; that commit deleted this file" if hit.file_deleted_by_commit else "")
        )
        for sample in hit.sample:
            print(f"    e.g. {sample[:120]}")
    _warn_unused(unused)
    if blocked:
        print()
        print(
            f"no-silent-resurrection: {len(blocked)} hit(s) resurrect main-deleted content "
            f"({len(allowed)} allowed)."
        )
        _line_recipe(blocked[0])
        return 1
    print(f"no-silent-resurrection: {len(allowed)} hit(s), all allowed; nothing blocks this PR.")
    return 0


def _merge_base(base: str, head: str, cwd: Path) -> str:
    for rev in (base, head):
        try:
            _git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", cwd=cwd)
        except GitError:
            raise GitError(
                f"'{rev}' is not a commit in this repository - check the ref "
                f"(a CI checkout needs fetch-depth: 0)"
            ) from None
    return _git("merge-base", base, head, cwd=cwd).strip()


if __name__ == "__main__":
    raise SystemExit(main())
