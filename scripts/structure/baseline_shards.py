"""The structure baseline, sharded by directory so unrelated changes touch different files.

`scripts/structure/baseline/<component>/<area>.json` groups frozen entries
by the first two components of their directory (`agent/graph` ->
`agent/graph.json`, `cli/commands/_x.py` -> `cli/commands.json`,
`scripts/lint_x.py` -> `scripts.json`). Historical flat shard names remain
readable. A shard maps section names to their entries and omits empty sections. One file per area
keeps concurrent PRs off each other's lines. Existing entries can stay in their
original shard when their files move; duplicate section/key pairs are errors.

`rules.json` is not a shard: it names the rule version each section was frozen under
(`{"patch_targets": 2}`; a section it does not list is at version 1). A rule version records a measurement change,
but it does not relax the shrink-only guard: existing keys may only disappear
or decrease. Neither introducing a lint nor changing its version permits new
baseline targets.

The directory also carries `README.md`, committed even when every shard is
empty (all debt paid off): git does not track empty directories, so without it
a fully clean baseline would vanish from the tree and become indistinguishable
from a revision that predates the sharded baseline entirely.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import cast

SHARD_DIR = "scripts/structure/baseline"
RULES_FILE = "rules.json"
_SHARD_DEPTH = 2


def shard_of(kind: str, key: str) -> str:
    """The shard name for one entry: its directory's first two components, slash-joined.

    A `directories` key is itself the directory; every other key names a file
    (`path` or `path::target`) whose parent directory decides.
    """
    path = PurePosixPath(key.split("::", 1)[0])
    directory = path if kind == "directories" else path.parent
    return "/".join(directory.parts[:_SHARD_DEPTH])


def shard_path(kind: str, key: str) -> str:
    """The repo-relative shard file an entry belongs in (for error messages)."""
    return f"{SHARD_DIR}/{shard_of(kind, key)}.json"


def split(baseline: dict[str, dict[str, int]]) -> dict[str, dict[str, dict[str, int]]]:
    """Distribute a merged baseline over its shards (empty sections omitted)."""
    shards: dict[str, dict[str, dict[str, int]]] = {}
    for kind, entries in baseline.items():
        for key, count in entries.items():
            shards.setdefault(shard_of(kind, key), {}).setdefault(kind, {})[key] = count
    return shards


def render(shard: dict[str, dict[str, int]]) -> str:
    """One shard's canonical text: sorted sections and keys, 2-space JSON."""
    ordered = {kind: dict(sorted(shard[kind].items())) for kind in sorted(shard)}
    return json.dumps(ordered, indent=2) + "\n"


def merge(texts: dict[str, str], sections: Iterable[str]) -> dict[str, dict[str, int]]:
    """Merge shard texts (name -> JSON) into one baseline carrying every section.

    Raises ValueError on a malformed shard, an unknown section, or an entry
    present in two shards. Shard names do not grant additional permissions.
    """
    merged: dict[str, dict[str, int]] = {kind: {} for kind in sections}
    for name, text in sorted(texts.items()):
        shard = json.loads(text)
        if not isinstance(shard, dict):
            raise ValueError(f"shard {name}.json must be an object")  # noqa: TRY004 — schema error
        for kind, entries in shard.items():
            if kind not in merged:
                raise ValueError(f"shard {name}.json has unknown section {kind!r}")
            if not isinstance(entries, dict):
                raise ValueError(f"shard {name}.json section {kind!r} must be an object")  # noqa: TRY004
            for key, count in entries.items():
                if key in merged[kind]:
                    raise ValueError(
                        f"shard {name}.json duplicates {kind} entry {key!r} from another shard"
                    )
                merged[kind][key] = count
    return merged


def read_worktree(repo_root: Path) -> dict[str, str]:
    """Every shard (`*.json`) in the working tree, by name.

    The shard directory always carries `README.md`, even with zero shards (an
    all-paid-off baseline) — that keeps git tracking the directory, so its
    absence here can only be an accidental deletion. Fail fast rather than
    silently treating a missing directory as an empty baseline.
    """
    directory = repo_root / SHARD_DIR
    if not directory.is_dir():
        raise ValueError(
            f"baseline directory missing: {SHARD_DIR} (its README.md keeps it tracked)"
        )
    return {
        path.relative_to(directory).with_suffix("").as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*.json"))
        if path.relative_to(directory).as_posix() != RULES_FILE
    }


def _tree_listing(repo_root: Path, rev: str, *, recursive: bool) -> bytes:
    options = ["-r", "--name-only"] if recursive else []
    return subprocess.run(  # noqa: S603 — local git query, no shell
        ["git", "-C", str(repo_root), "ls-tree", "-z", *options, rev, "--", SHARD_DIR],
        capture_output=True,
        check=True,
    ).stdout


def _historical_paths(repo_root: Path, rev: str) -> list[str] | None:
    listing = _tree_listing(repo_root, rev, recursive=True)
    if not listing:
        entry = _tree_listing(repo_root, rev, recursive=False)
        if not entry:
            return None
        metadata, _, name = entry.partition(b"\t")
        fields = metadata.split()
        if len(fields) != 3 or fields[1] != b"tree" or name != f"{SHARD_DIR}\0".encode():
            raise ValueError(f"invalid baseline directory at {rev}")
        return []
    paths = listing.decode().split("\0")
    prefix = f"{SHARD_DIR}/"
    if paths[-1] != "" or any(not path.startswith(prefix) for path in paths[:-1]):
        raise ValueError(f"invalid baseline tree listing at {rev}")
    return [path.removeprefix(prefix) for path in paths[:-1]]


def _batch_texts(data: bytes, filenames: list[str]) -> dict[str, str]:
    texts: dict[str, str] = {}
    cursor = 0
    for filename in filenames:
        header_end = data.find(b"\n", cursor)
        if header_end < 0:
            raise ValueError(f"truncated Git batch header for {filename}")
        header = data[cursor:header_end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError(f"unreadable Git blob for {filename}: {data[cursor:header_end]!r}")
        if len(header[0]) not in (40, 64) or any(
            char not in b"0123456789abcdef" for char in header[0]
        ):
            raise ValueError(f"invalid Git object ID for {filename}")
        size = int(header[2])
        body_start = header_end + 1
        body_end = body_start + size
        if size < 0 or data[body_end : body_end + 1] != b"\n":
            raise ValueError(f"invalid Git batch body for {filename}")
        texts[filename.removesuffix(".json")] = data[body_start:body_end].decode()
        cursor = body_end + 1
    if cursor != len(data):
        raise ValueError("unexpected data after Git baseline batch")
    return texts


def read_at(repo_root: Path, rev: str) -> dict[str, str] | None:
    """Every shard (`*.json`) at a revision, by name.

    None only when a valid revision's tree has no shard directory at all — the
    one-time migration commit that predates the sharded baseline, where the
    guard skips itself rather than comparing against nothing. Once the
    directory exists (its README.md keeps it tracked even with zero shards),
    this returns a dict, possibly empty — a real empty baseline, compared
    against normally and not treated as guard-skip territory. Git failures and
    unreadable or incomplete blobs raise instead of disabling the guard.
    """

    paths = _historical_paths(repo_root, rev)
    if paths is None:
        return None
    filenames = [name for name in paths if name.endswith(".json") and name != RULES_FILE]
    if not filenames:
        return {}
    # One `cat-file --batch` for every shard: a process per shard costs ~30ms each, and the
    # baseline has dozens of them.
    batch = subprocess.run(  # noqa: S603 — local git query, no shell
        ["git", "-C", str(repo_root), "cat-file", "--batch"],
        input="".join(f"{rev}:{SHARD_DIR}/{name}\n" for name in filenames).encode(),
        capture_output=True,
        check=True,
    )
    return _batch_texts(batch.stdout, filenames)


def parse_rules(text: str) -> dict[str, int]:
    """The rule versions of a `rules.json` text: section -> integer version >= 1."""
    rules = cast("object", json.loads(text))
    if not isinstance(rules, dict) or any(
        type(version) is not int or version < 1
        for version in cast("dict[str, object]", rules).values()
    ):
        raise ValueError(f"{RULES_FILE} must map a section to an integer rule version >= 1")
    return cast("dict[str, int]", rules)


def read_rules_worktree(repo_root: Path) -> dict[str, int]:
    """The rule versions in the working tree (none: every section is at version 1)."""
    path = repo_root / SHARD_DIR / RULES_FILE
    return parse_rules(path.read_text(encoding="utf-8")) if path.is_file() else {}


def read_rules_at(repo_root: Path, rev: str) -> dict[str, int]:
    """Rule versions at a valid revision; a missing file means version 1.

    Revision resolution and Git reads must succeed before absence is accepted.
    """
    paths = _historical_paths(repo_root, rev)
    if paths is None or RULES_FILE not in paths:
        return {}
    shown = subprocess.run(  # noqa: S603 — local git query, no shell
        ["git", "-C", str(repo_root), "show", f"{rev}:{SHARD_DIR}/{RULES_FILE}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return parse_rules(shown.stdout)


def read_rules(repo_root: Path, rev: str) -> tuple[dict[str, int], dict[str, int]]:
    """(the rule versions at `rev`, the rule versions in the working tree)."""
    return read_rules_at(repo_root, rev), read_rules_worktree(repo_root)
