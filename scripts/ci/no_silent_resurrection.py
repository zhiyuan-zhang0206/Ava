#!/usr/bin/env python3
"""Reject recent main deletions restored without an explicit declaration.

Pipeline and limits: docs/conventions/agents/no-silent-resurrection.md.
Proven retained keyword continuations are syntax provenance, not behavioral
approval. Stdlib/Git only. Exit codes: 0 clean/allowed, 1 blocked, 2 Git/input error.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE = "origin/main"
DEFAULT_HEAD = "HEAD"
DEFAULT_DAYS = 30

# Strong lines carry identifier evidence; three-line blocks or a distinctive
# operation are decisive. See the convention for the historical noise sweep.
MIN_STRONG_LENGTH = 14
MAX_STRONG_LENGTH = 1000
MIN_IDENTIFIERS = 2
MIN_IDENTIFIER_LENGTH = 3
DISTINCTIVE_IDENTIFIER_LENGTH = 20
MIN_DEAD_LINES = 3
SAMPLE_LINES = 3

SKIPPED_FILES = frozenset(
    {
        "uv.lock",
        "ui/web/package-lock.json",
        "ui/web/openapi.json",
        "ui/web/src/lib/contracts/types-generated.ts",
        "base/host/env/config_lite_table.json",
        "base/events/registry.md",
        "db/schema.sql",
        "scripts/zombie_pyright_ignores.registry",
        "base/agents/api.txt",
        "base/events/api.txt",
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


def _qualified_type_reference(tokens: list[tokenize.TokenInfo], index: int) -> bool:
    """Recognize a CamelCase attribute reference without invoking its constructor."""
    name = tokens[index].string
    return (
        index > 0
        and tokens[index - 1].string == "."
        and name[0].isupper()
        and "_" not in name
        and _CAMEL_CASE.search(name) is not None
        and index + 1 < len(tokens)
        and tokens[index + 1].string != "("
    )


def _has_distinctive_identifier(text: str) -> bool:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError):
        # Incomplete or non-Python fragments retain the conservative text rule.
        return any(
            len(name) >= DISTINCTIVE_IDENTIFIER_LENGTH and ("_" in name or _CAMEL_CASE.search(name))
            for name in _IDENTIFIER.findall(text)
        )
    for index, token in enumerate(tokens):
        name = token.string
        if token.type != tokenize.NAME:
            continue
        if (
            not _qualified_type_reference(tokens, index)
            and len(name) >= DISTINCTIVE_IDENTIFIER_LENGTH
            and ("_" in name or _CAMEL_CASE.search(name))
        ):
            return True
    return False


def _exception_handler_header(text: str) -> bool:
    """A header naming exception types does not contain the handler's operation."""
    if not text.startswith(("except ", "except* ")) or not text.endswith(":"):
        return False
    try:
        tree = ast.parse(f"try:\n    pass\n{text}\n    pass\n")
    except SyntaxError:
        return False
    statement = tree.body[0]
    if not isinstance(statement, (ast.Try, ast.TryStar)):
        return False
    exception_type = statement.handlers[0].type
    return exception_type is not None and all(
        isinstance(node, (ast.Name, ast.Attribute, ast.Tuple, ast.Load))
        for node in ast.walk(exception_type)
    )


def classify(text: str) -> tuple[bool, bool]:
    """Return (strong, distinctive) for one normalized added line."""
    if not MIN_STRONG_LENGTH <= len(text) <= MAX_STRONG_LENGTH:
        return False, False
    if text.startswith(_COMMENT_PREFIXES) or text.startswith(_IMPORT_PREFIXES):
        return False, False
    if not re.search(r"[A-Za-z0-9]", text):
        return False, False
    identifiers = [t for t in _IDENTIFIER.findall(text) if len(t) >= MIN_IDENTIFIER_LENGTH]
    strong = len(identifiers) >= MIN_IDENTIFIERS
    # An opening prefix or declaration omits the implementation of its contract.
    # Keep it strong for restored blocks, but never decisive on its own.
    declaration = text.startswith(("def ", "async def ", "class ")) and text.endswith(":")
    return (
        strong,
        strong
        and not text.endswith("(")
        and not declaration
        and not _exception_handler_header(text)
        and _has_distinctive_identifier(text),
    )


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
                strong, distinctive = classify(text)
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


def alive_candidates(candidates: set[str], base: str, cwd: Path) -> set[str]:
    """Candidate texts still present anywhere in the base tree (skipped paths excluded)."""
    if not candidates:
        return set()
    # Git owns binary detection and exclusions; membership remains whole-line.
    stream = subprocess.Popen(  # noqa: S603
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
            "-e",
            "",
            base,
            "--",
            ".",
            *[f":(exclude){path}" for path in sorted(SKIPPED_FILES)],
            *[f":(exclude){directory}" for directory in SKIPPED_DIRS],
        ],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = stream.stdout
    if stdout is None:
        raise GitError("git grep produced no output stream")
    alive: set[str] = set()
    encoded_candidates = tuple(text.encode("utf-8") for text in candidates)
    for raw in stdout:
        for line in raw.decode("utf-8", errors="replace").splitlines():
            text = line.strip()
            if text not in candidates:
                continue
            # Preserve byte-pattern eligibility for replacement-decoded UTF-8.
            if "\ufffd" in text and not any(pattern in raw for pattern in encoded_candidates):
                continue
            alive.add(text)
    _, stderr = stream.communicate()
    if stream.returncode > 1:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise GitError(f"`git grep` over {base} failed: {detail[0] if detail else 'exit'}")
    return alive


def _syntax(node: ast.AST) -> str:
    return ast.dump(node)


def _tokens(text: str | None) -> tuple[tuple[int, str], ...]:
    if text is None:
        raise ValueError("AST source span is missing")
    ignored = {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
    return tuple(
        (token.type, token.string)
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type not in ignored
    )


def _named_parts(call: ast.Call, source: str) -> list[tuple[tuple[int, str], ...]] | None:
    names = [keyword.arg for keyword in call.keywords]
    if None in names or len(set(names)) != len(names):
        return None
    if any(isinstance(arg, ast.Starred) for arg in call.args):
        return None
    parts = [_tokens(ast.get_source_segment(source, k)) for k in call.keywords]
    return parts if all(parts) else None


def _retained_call(old: ast.Call, new: ast.Call, before: str, after: str) -> bool:
    """Only additive named inputs; no inference about the callee's behavior."""
    old_parts, new_parts = _named_parts(old, before), _named_parts(new, after)
    if not old_parts or new_parts is None or _syntax(old.func) != _syntax(new.func):
        return False
    if _syntax(ast.Tuple(elts=old.args)) != _syntax(ast.Tuple(elts=new.args)):
        return False
    names = {part[0][1] for part in old_parts}
    return old_parts == [part for part in new_parts if part[0][1] in names]


def _direct_calls(tree: ast.Module) -> dict[tuple[str, int], tuple[ast.Assign, str]]:
    """No nested scopes or control-flow correspondence heuristics."""
    owners = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    names = Counter(node.name for node in owners)
    calls: dict[tuple[str, int], tuple[ast.Assign, str]] = {}
    for owner in owners:
        counts = Counter(_syntax(n.func) for n in ast.walk(owner) if isinstance(n, ast.Call))
        for index, statement in enumerate(owner.body):
            if names[owner.name] != 1 or not isinstance(statement, ast.Assign):
                continue
            if isinstance(statement.value, ast.Call) and counts[_syntax(statement.value.func)] == 1:
                calls[(owner.name, index)] = (
                    statement,
                    str([_syntax(n) for n in [*owner.decorator_list, *owner.body[:index]]]),
                )
    return calls


def _retained_keywords(before: str, after: str) -> set[int]:
    """Locations of whole keyword lines retained at a proven direct assignment site."""
    old_tree, new_tree = ast.parse(before), ast.parse(after)
    old_calls, new_calls = _direct_calls(old_tree), _direct_calls(new_tree)
    retained: set[int] = set()
    lines = after.splitlines()
    for site in old_calls.keys() & new_calls.keys():
        (old, old_prefix), (new, new_prefix) = old_calls[site], new_calls[site]
        if _syntax(ast.Tuple(elts=old.targets)) != _syntax(ast.Tuple(elts=new.targets)):
            continue
        if old_prefix != new_prefix:
            continue
        if not isinstance(old.value, ast.Call) or not isinstance(new.value, ast.Call):
            continue
        if not _retained_call(old.value, new.value, before, after):
            continue
        originals = {k.arg for k in old.value.keywords}
        for keyword in new.value.keywords:
            if keyword.arg not in originals or keyword.lineno != keyword.end_lineno:
                continue
            part = _tokens(ast.get_source_segment(after, keyword))
            line = _tokens(lines[keyword.lineno - 1].strip())
            if part and line in (part, (*part, (tokenize.OP, ","))):
                retained.add(keyword.lineno)
    return retained


def _retained_candidates(hits: list[Hit], base: str, head: str, cwd: Path) -> set[tuple[str, int]]:
    """Keep unsupported/ambiguous syntax on the original detection path."""
    retained: set[tuple[str, int]] = set()
    for path in sorted({hit.path for hit in hits if hit.path.endswith(".py")}):
        if not _git("ls-tree", base, "--", path, cwd=cwd).strip():
            continue  # A genuinely new file has no retained site.
        before = _git("show", f"{base}:{path}", cwd=cwd)
        after = _git("show", f"{head}:{path}", cwd=cwd)
        try:
            retained.update((path, line) for line in _retained_keywords(before, after))
        except (SyntaxError, tokenize.TokenError, IndentationError, ValueError) as error:
            print(
                f"no-silent-resurrection: unresolved syntax in {path}: {error}; raw detection retained"
            )
    return retained


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
    """Stream recent main deletions; content re-added in the same commit stays live."""
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


def _find_hits(
    runs: list[Run],
    alive: set[str],
    dead: dict[str, list[Commit]],
    retained: set[tuple[str, int]] | None = None,
) -> list[Hit]:
    hits: list[Hit] = []
    for run in runs:
        segment: list[Line] = []
        for line in run.lines:
            if line.strong and line.text not in alive and line.text not in dead:
                hits.extend(_segment_hits(run.path, segment, alive, dead, retained or set()))
                segment = []
                continue
            segment.append(line)
        hits.extend(_segment_hits(run.path, segment, alive, dead, retained or set()))
    return hits


def _segment_hits(
    path: str,
    segment: list[Line],
    alive: set[str],
    dead: dict[str, list[Commit]],
    retained: set[tuple[str, int]],
) -> list[Hit]:
    dead_lines = [
        line
        for line in segment
        if line.strong
        and line.text in dead
        and line.text not in alive
        and (path, line.number) not in retained
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


def declared_targets(reason: str) -> tuple[list[str], list[str]]:
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
            shas, paths = declared_targets(match.group(1))
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
    if allowance.shas and not any(hit.commit.sha.startswith(sha) for sha in allowance.shas):
        return False
    if allowance.paths:
        return any(_path_matches(declared, hit.path) for declared in allowance.paths)
    return bool(allowance.shas)


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
    print("\nno-silent-resurrection: intentional restoration requires a commit declaration:")
    print(f"    Resurrects: {hit.commit.sha[:9]} {hit.path}")
    print("Both targets must match. See docs/conventions/agents/no-silent-resurrection.md")
    print("for SHA-only, path-only, broad declarations and explicit revert semantics.")


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
        alive = alive_candidates(candidates, args.base, cwd)
        dead, scanned = _deleted_candidates(args.base, args.days, candidates, cwd)
        hits = _find_hits(runs, alive, dead)
        retained = _retained_candidates(hits, merge_base, args.head, cwd)
        hits = _find_hits(runs, alive, dead, retained)
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
    print(
        f"no-silent-resurrection: {len(retained)} retained keyword line(s); other syntax keeps raw detection"
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
