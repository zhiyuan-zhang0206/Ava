"""The structure baseline, sharded by directory so unrelated changes touch different files.

`scripts/structure/baseline/<shard>.json` holds every frozen entry whose path
lives in that shard's directory: the entry's directory cut to its first two
components (`agent/graph` -> `agent.graph.json`, `cli/commands/_x.py` ->
`cli.commands.json`, `scripts/lint_x.py` -> `scripts.json`). A shard maps
section names to their entries and omits empty sections. One file per area
keeps concurrent PRs off each other's lines; an entry filed under the wrong
shard is an error, so every entry has exactly one home.

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

SHARD_DIR = "scripts/structure/baseline"
_SHARD_DEPTH = 2


def shard_of(kind: str, key: str) -> str:
    """The shard name for one entry: its directory's first two components, dot-joined.

    A `directories` key is itself the directory; every other key names a file
    (`path` or `path::target`) whose parent directory decides.
    """
    path = PurePosixPath(key.split("::", 1)[0])
    directory = path if kind == "directories" else path.parent
    return ".".join(directory.parts[:_SHARD_DEPTH])


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

    Raises ValueError on a malformed shard, an unknown section, an entry filed
    under the wrong shard, or an entry present in two shards.
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
                home = shard_of(kind, key)
                if home != name:
                    raise ValueError(
                        f"shard {name}.json holds {kind} entry {key!r}, which belongs in {home}.json"
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
        path.stem: path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.json"))
    }


def read_at(repo_root: Path, rev: str) -> dict[str, str] | None:
    """Every shard (`*.json`) at a revision, by name.

    None only when the revision's tree has no shard directory at all — the
    one-time migration commit that predates the sharded baseline, where the
    guard skips itself rather than comparing against nothing. Once the
    directory exists (its README.md keeps it tracked even with zero shards),
    this returns a dict, possibly empty — a real empty baseline, compared
    against normally and not treated as guard-skip territory.
    """

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — local git query, no shell
            ["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False
        )

    listing = git("ls-tree", "--name-only", f"{rev}:{SHARD_DIR}")
    if listing.returncode:
        return None
    texts: dict[str, str] = {}
    for filename in listing.stdout.split():
        if not filename.endswith(".json"):
            continue
        shown = git("show", f"{rev}:{SHARD_DIR}/{filename}")
        if shown.returncode:
            return None
        texts[filename.removesuffix(".json")] = shown.stdout
    return texts
