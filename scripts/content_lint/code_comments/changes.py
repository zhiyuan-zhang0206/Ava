import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from base.host.proc import run_bounded
from scripts.content_lint.code_comments.check import Repository
from scripts.content_lint.code_comments.model import NoteKind, Source
from scripts.content_lint.code_comments.python_source import parse_python
from scripts.content_lint.code_comments.typescript_index import SUFFIXES, parsed_sources


def _brand_key(text: str) -> str:
    return (
        text.replace("meiva", "a" + "va").replace("Meiva", "A" + "va").replace("MEIVA", "A" + "VA")
    )


@dataclass(frozen=True)
class Snapshot:
    before: dict[str, str]
    after: dict[str, str]


def git(root: Path, *args: str) -> str:
    result = run_bounded(["git", *args], cwd=root, timeout=30, capture_output=True)
    if result.returncode:
        raise ValueError(f"Git comparison failed: {result.stderr.decode('utf-8').strip()}")
    return result.stdout.decode("utf-8")


def comparison_base(
    root: Path, explicit: str | None, start: str | None, end: str | None
) -> tuple[str, bool]:
    if explicit is not None:
        if start or end:
            raise ValueError("Choose a base or a from/to range, not both")
        return git(
            root, "rev-parse", "--verify", "--end-of-options", f"{explicit}^{{commit}}"
        ).strip(), False
    if start or end:
        if not start or not end:
            raise ValueError("Code comment comparison requires both from-ref and to-ref")
        base = git(root, "rev-parse", "--verify", "--end-of-options", f"{start}^{{commit}}").strip()
        target = git(root, "rev-parse", "--verify", "--end-of-options", f"{end}^{{commit}}").strip()
        if target != git(root, "rev-parse", "HEAD").strip():
            raise ValueError("Code comment range must end at the checked-out HEAD")
        return base, False
    return git(root, "rev-parse", "--verify", "HEAD").strip(), True


def require_snapshot(root: Path, *, staged: bool) -> None:
    arguments: list[tuple[str, ...]] = [("diff", "--quiet", "--no-ext-diff", "--")]
    if not staged:
        arguments.append(("diff", "--cached", "--quiet", "--no-ext-diff", "HEAD", "--"))
    for args in arguments:
        result = run_bounded(["git", *args], cwd=root, timeout=30, capture_output=True)
        if result.returncode:
            raise ValueError(
                "Code comment snapshot mismatch: tracked working tree must match the index, "
                "and committed comparisons require index and working tree to match HEAD; use the normal hook isolation"
            )


def tree_entries(root: Path, revision: str | None) -> dict[str, tuple[str, str]]:
    args = ("ls-files", "--stage", "-z") if revision is None else ("ls-tree", "-r", "-z", revision)
    entries: dict[str, tuple[str, str]] = {}
    for record in git(root, *args).split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        fields = metadata.split()
        if revision is None and fields[2] != "0":
            raise ValueError(f"unmerged index path: {path}")
        entries[path] = (fields[0], fields[1] if revision is None else fields[2])
    return entries


# meiva:ref scripts/content_lint/code_comments/docs/incremental.meiva.okf.md#git-snapshot
def snapshot(
    repo: Repository,
    base: str | None = None,
    *,
    from_ref: str | None = None,
    to_ref: str | None = None,
) -> Snapshot:
    revision, staged = comparison_base(repo.root, base, from_ref, to_ref)
    require_snapshot(repo.root, staged=staged)
    args = ("diff", "--cached", revision) if staged else ("diff", revision, "HEAD")
    paths = [
        path
        for path in git(
            repo.root, *args, "--no-renames", "--name-only", "-z", "--diff-filter=ACMRT", "--"
        ).split("\0")
        if path.endswith((".py", *SUFFIXES))
    ]
    old = tree_entries(repo.root, revision)
    new = tree_entries(repo.root, None if staged else "HEAD")
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for path in paths:
        repo.text(path)
        mode, blob = new[path]
        if mode not in {"100644", "100755"}:
            raise ValueError(f"not a regular source blob: {path}")
        after[path] = git(repo.root, "cat-file", "blob", blob)
        old_mode, old_blob = old.get(path, ("", ""))
        if not old_mode and _brand_key(path) != path:
            old_mode, old_blob = old.get(_brand_key(path), ("", ""))
        if old_mode in {"100644", "100755"}:
            before[path] = git(repo.root, "cat-file", "blob", old_blob)
    return Snapshot(before, after)


def parse_blobs(root: Path, contents: dict[str, str]) -> dict[str, Source]:
    sources = {
        path: parse_python(path, text) for path, text in contents.items() if path.endswith(".py")
    }
    scripts = {path: text for path, text in contents.items() if path.endswith(SUFFIXES)}
    if scripts:
        result = run_bounded(
            ["node", str(Path(__file__).with_name("typescript.mjs")), "--sources-json"],
            cwd=root,
            input=json.dumps(scripts),
            text=True,
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            raise ValueError(f"TypeScript blob parser failed: {result.stderr.strip()}")
        sources.update(parsed_sources(result.stdout, scripts))
    return sources


# meiva:ref scripts/content_lint/code_comments/docs/incremental.meiva.okf.md#new-note-scope
def new_note_indexes(source: Source, previous: Source | None) -> frozenset[int]:
    remaining: Counter[tuple[str, str, NoteKind, str]] = (
        Counter(
            (
                _brand_key(previous.path),
                _brand_key(note.lexical_owner),
                note.kind,
                _brand_key(note.raw_text),
            )
            for note in previous.notes
        )
        if previous is not None
        else Counter()
    )
    selected: set[int] = set()
    for index, note in enumerate(source.notes):
        key = (
            _brand_key(source.path),
            _brand_key(note.lexical_owner),
            note.kind,
            _brand_key(note.raw_text),
        )
        if remaining[key]:
            remaining[key] -= 1
        else:
            selected.add(index)
    return frozenset(selected)


def incremental_notes(
    repo: Repository,
    base: str | None = None,
    *,
    from_ref: str | None = None,
    to_ref: str | None = None,
) -> dict[str, frozenset[int]]:
    comparison = snapshot(repo, base, from_ref=from_ref, to_ref=to_ref)
    before = parse_blobs(repo.root, comparison.before)
    after = parse_blobs(repo.root, comparison.after)
    repo.sources.update(after)
    return {path: new_note_indexes(source, before.get(path)) for path, source in after.items()}
