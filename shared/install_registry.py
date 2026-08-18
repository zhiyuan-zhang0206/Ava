"""Local install registry — every package in the skills load dir and its state.

A per-machine JSON file at `$AVA_HOME/installed.json` recording every package
present in the single skills load dir (`$AVA_HOME/skills/`), whether converged
from this repo / a plugin or installed from an external source (git URL /
marketplace). File-based and machine-local by design: there is no
cluster-shared row. `installed` means the package lives on disk; `enabled` is
a local on/off toggle the skill scanner honors — the two are orthogonal.

Distinct from `shared/plugins_config.py` (per-machine JSON plugin enable) and
`shared/config.py` (env-driven Settings): this is the decentralized-local
target for install state. The skill scanner loads from `~/.ava/skills/` only
entries this registry tracks as enabled (see `enabled_skill_names`), which is
what keeps stray copies from silently loading or shadowing converged skills.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Generator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from shared import paths
from shared.platform import LockTimeoutError as LockTimeoutError
from shared.platform import file_lock
from shared.skill_names import match_key

# Junk that must affect neither a package's tree hash nor its synced copy —
# otherwise e.g. a __pycache__ appearing in a source tree would read as a
# content change. Shared by skills converge (which writes `content_hash`) and
# every drift check that recomputes it. `.preserved` is the local-content
# protection marker (see `preserved_subpaths`) — a marker, not content.
IGNORED_NAMES = frozenset({"__pycache__", ".git", ".DS_Store", ".preserved"})

# The marker file a local adapter drops inside a managed package to protect
# itself from converge re-syncs: a subtree under the load dir carrying this
# marker is never deleted or overwritten, even when its parent package is
# re-copied from source (local adapters — e.g. intel's `web-sources/people` +
# `web-sources/rmrb` — live inside a repo-managed package directory, and a
# wholesale re-copy would silently drop them). Marker-based, not a global
# registry: a new adapter is protected by creating the marker alongside it,
# so there is no central list to forget to extend (audit round 2, #11).
PRESERVED_MARKER = ".preserved"


def preserved_subpaths(dest: Path) -> frozenset[tuple[str, ...]]:
    """Subtrees under `dest` carrying a `PRESERVED_MARKER` file, as
    path-part tuples relative to `dest` (what `tree_hash` skip expects).

    A marker at the package root itself is ignored (it would protect the whole
    package, which converge's bootstrap-only + update-contract already do).
    A preserved subtree is invisible to drift detection (it does not freeze
    its parent package) and survives `_copy_tree` replacement."""
    if not dest.is_dir():
        return frozenset()
    found: set[tuple[str, ...]] = set()
    for marker in dest.rglob(PRESERVED_MARKER):
        rel = marker.parent.relative_to(dest)
        if rel != Path():
            found.add(tuple(rel.parts))
    return frozenset(found)


def tree_hash(root: Path, *, skip_subtrees: frozenset[tuple[str, ...]] = frozenset()) -> str:
    """Deterministic content hash of every file under `root` (sorted relative
    path + bytes), ignoring `IGNORED_NAMES` and any `skip_subtrees` (path-part
    tuples relative to `root` — the preserved-local-content carve-out).

    This is the value stored in a package's `content_hash` at converge time; a
    later recompute that differs is the "modified locally" signal (a user edited
    the converge-managed copy). Cheap at this scale (a few MB of markdown)."""
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root)
        if any(part in IGNORED_NAMES for part in rel.parts):
            continue
        if any(rel.parts[: len(sp)] == sp for sp in skip_subtrees):
            continue
        if not f.is_file():
            continue
        h.update(rel.as_posix().encode())
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


PackageType = Literal["skill", "plugin", "mcp"]

TrustTier = Literal["builtin", "reviewed", "unreviewed"]
"""How much a package's *content* may be trusted, recorded where it enters:

- "builtin" — the content came from this Ava checkout (`ava_builtins/`), so it
  ships under the same review as the code. Written by converge.
- "reviewed" — third-party content a human on this cluster read and approved
  (`ava skill trust <name>`). Never inferred: a clean
  `shared.skill_scan` report is "no rule matched", not "someone looked".
- "unreviewed" — the default for anything ingested from outside. Installed and
  usable by an agent that deliberately opens it, but any runtime layer that
  pulls skill text into a context *without* a human in the loop (skill recall)
  must treat it as attacker-controlled.

The default is the least-trusted tier, so a registry row written before this
field existed reads as third-party until the next converge re-stamps it.
"""

PackageOrigin = Literal["repo", "plugin", "user"]
"""Who owns a package's content in `~/.ava/skills/`:

- "repo" / "plugin" — converge-managed derived state, synced from a source
  tree (`<repo>/ava_builtins/skills/`, `<repo>/ava_builtins/plugins/<p>/skills/`, or an installed
  plugin's `~/.ava/plugins/<p>/skills/`). Converge overwrites it when the
  source changes and removes it when the source disappears — unless the user
  hand-edited the copy (`content_hash` mismatch), which converge never
  clobbers.
- "user" — installed or registered by the user; converge never touches it.
"""


class InstalledPackage(BaseModel):
    """One tracked package.

    `source` is the git URL / marketplace ref it was installed from (None for
    converge-managed and hand-registered packages); `path` is the subdirectory
    within that source repo holding the package (None = repo root — a
    marketplace plugin lives at e.g. `plugins/<name>/` in a monorepo). `ref`
    pins a tag / commit / branch (None = source default branch). `enabled` is
    a local toggle — disabled skills stay on disk but are not surfaced.

    `origin` / `origin_path` record converge provenance (see `PackageOrigin`);
    `content_hash` is the tree hash of what converge last wrote (its
    user-modification detector). `installed_at` / `updated_at` are UTC ISO
    timestamps.

    `trust` is the content trust tier (see `TrustTier`); `scanned_at` is when
    `shared.skill_scan` last ran over it, and `accepted_findings` holds the rule
    ids a human waved through with `--accept-risk`. An accepted-risk package
    stays `unreviewed` — the override records a decision, it does not promote.
    """

    name: str
    type: PackageType
    source: str | None = None
    path: str | None = None
    ref: str | None = None
    enabled: bool = True
    origin: PackageOrigin = "user"
    origin_path: str | None = None
    content_hash: str | None = None
    installed_hash: str | None = None
    """Tree hash of the package directory as the last install/upgrade wrote it
    (R5). Distinct from `content_hash`, which converge owns for load-dir
    copies: converge overwrites `content_hash` on installed-plugin rows (it
    records the *skills-copy* hash), so update/upgrade conflict detection
    against the *package* tree reads this field instead. A None here (legacy
    rows) counts as changed — see `copy_changed`."""
    installed_at: str | None = None
    updated_at: str | None = None
    trust: TrustTier = "unreviewed"
    scanned_at: str | None = None
    accepted_findings: list[str] = []


class Registry(BaseModel):
    """The whole `installed.json` — a flat list of packages keyed by name.

    `version` is the registry schema version (absent/1 on legacy files, default
    1) — the migration anchor if the schema ever evolves (the sibling
    `~/.agents/.skill-lock.json` format already carries one; audit round 2,
    skills-plugins #14)."""

    version: int = 1
    packages: list[InstalledPackage] = []


class InstallRegistryError(Exception):
    """Root of install-registry read / validation failures."""


class SchemaInvalid(InstallRegistryError):  # noqa: N818
    """installed.json is malformed JSON or does not match the Registry schema."""


class DuplicatePackageName(InstallRegistryError):  # noqa: N818
    """Two registry rows fold to the same package key.

    Dash and underscore are one name (`shared.skill_names.match_key`), so
    `ava-code` and `ava_code` as separate rows are the same package twice —
    the dual-row state that made the skill scanner crash fleet-wide (audit
    02 #4). One package must have exactly one row (design R2-B1), so the
    registry read refuses it rather than letting both rows load.
    """


def _folding_duplicates(registry: Registry) -> list[tuple[str, str]]:
    """Pairs of row names that fold to the same key, in load order.

    Returns [] when every row folds to a distinct key. Used by `load` to
    fail fast and by the skill-identity migration tool to report / merge."""
    seen: dict[str, str] = {}
    dups: list[tuple[str, str]] = []
    for pkg in registry.packages:
        key = match_key(pkg.name)
        prev = seen.get(key)
        if prev is not None:
            dups.append((prev, pkg.name))
        else:
            seen[key] = pkg.name
    return dups


def load() -> Registry:
    """Read `$AVA_HOME/installed.json`; a missing or empty file is an empty registry.

    Raises:
        SchemaInvalid: file exists but JSON / schema is invalid (fail-fast —
            don't silently drop a corrupt registry).
        DuplicatePackageName: two rows fold to the same package key (the
            dual-row state that used to crash the skill scanner).
        OSError: filesystem read error.
    """
    path = paths.install_registry_path()
    if not path.exists():
        return Registry()
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return Registry()
    try:
        registry = Registry.model_validate_json(raw)
    except ValidationError as e:
        raise SchemaInvalid(f"{path} schema invalid: {e}") from e
    dups = _folding_duplicates(registry)
    if dups:
        names = ", ".join(f"{a!r} / {b!r}" for a, b in dups)
        raise DuplicatePackageName(
            f"{path} has rows that fold to the same package key: {names} — "
            "dash and underscore are one name; merge them (see "
            "scripts/migrate_skill_identity.py)"
        )
    return registry


def save(registry: Registry) -> None:
    """Full-replace `$AVA_HOME/installed.json` with pretty JSON, atomically.

    The body is written to a temp sibling and renamed over the real path, so a
    crash mid-write can never leave a truncated registry that reads as an empty
    one (the #974 trigger surface: a half-written installed.json silently
    dropping every package).

    Call this only from inside `mutate` — atomic is not the same as serialized,
    and this write needs both. The temp sibling has ONE fixed name (so a stale
    temp from a crashed writer is swept rather than accumulating), which means
    two savers running at once share it: the second overwrites the first's
    staged body, and whichever unlinks first leaves the other's rename raising
    FileNotFoundError. `mutate`'s lock is what keeps them from overlapping."""
    path = paths.install_registry_path()
    body = json.dumps(json.loads(registry.model_dump_json()), indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def get(name: str) -> InstalledPackage | None:
    """Return the tracked package with this name, or None.

    Name matching is dash/underscore-insensitive (`shared.skill_names`): a row
    written before the dash rename and a caller asking for the dash spelling
    mean one package,
    so a rename does not orphan a registry row mid-upgrade."""
    return next((p for p in load().packages if match_key(p.name) == match_key(name)), None)


# Bound on the wait for the registry lock, in seconds. Every cycle it protects
# is a load, some local filesystem work over the skills tree, and a save —
# tens of milliseconds for a real registry (45 packages, measured), so a wait
# near this bound means a holder is wedged or a caller nested two cycles. Matches
# the `.env` bound (`runtime_config._ENV_LOCK_TIMEOUT_S`) — same shape of guarded
# section, same reason not to wait forever.
_REGISTRY_LOCK_TIMEOUT_S = 30.0


@contextlib.contextmanager
def registry_lock(registry_path: Path | None = None) -> Generator[None]:
    """Hold the cross-process lock guarding one `installed.json`.

    The lock file is a SIBLING, never the registry itself: `save` publishes by
    renaming a temp over the registry, so a lock on that inode would stop
    guarding anything the moment the first writer landed.

    `mutate` is the normal way in and takes this for you. It is exposed
    separately for the one writer that cannot use `mutate` — the out-of-band
    `scripts/migrate_skill_identity.py --apply`, which rewrites the registry
    under an arbitrary `--ava-home`, hence the explicit `registry_path`
    (defaulting to this unit's). Both must name the same lock file and share one
    bound, so both live here rather than being restated at the call site.
    """
    path = registry_path or paths.install_registry_path()
    with file_lock(path.with_name(f"{path.name}.lock"), timeout_s=_REGISTRY_LOCK_TIMEOUT_S):
        yield


@contextlib.contextmanager
def mutate() -> Generator[Registry]:
    """Load the registry, hand it over to be edited, and save it back — the one
    way to change `installed.json`.

    `save` is a full replace, so every change is a read-modify-write, and the
    writers are separate PROCESSES: `ava skill install` in an agent's shell,
    `ava converge` on a restart, the gateway's skills-toggle handler, and
    `scripts/migrate_skill_identity.py --apply`. Two of them racing lose one
    side's rows outright — whoever saves last wins with a registry it read
    before the others' rows existed. The package stops being tracked while its
    directory is still on disk, which is the state the skill scanner refuses to
    load.

    So the whole cycle runs under `registry_lock`. Expiry raises
    `LockTimeoutError` rather than saving anyway: a caller that could not take
    the lock never established what the lock is for. The body must not open a
    second cycle — the lock is per open-file-description and so not re-entrant,
    and a nested `mutate` (or a `register` / `deregister` called inside one)
    contends with its own outer take. That surfaces as `LockTimeoutError`, but
    only after the full `_REGISTRY_LOCK_TIMEOUT_S` — a nested cycle stalls its
    caller for 30 seconds before reporting. Bounded and loud, NOT fail-fast, so
    the way to catch one is a test or a review, not a fast red in production.
    An exception inside the body propagates with nothing saved, leaving the
    on-disk registry as it was.
    """
    with registry_lock():
        registry = load()
        yield registry
        save(registry)


def register(pkg: InstalledPackage) -> None:
    """Add the package, replacing any existing entry with the same name, then
    persist. Same-name is decided through `match_key`, so re-registering under
    the dash spelling REPLACES the underscore row rather than duplicating it."""
    with mutate() as registry:
        key = match_key(pkg.name)
        registry.packages = [p for p in registry.packages if match_key(p.name) != key]
        registry.packages.append(pkg)


def deregister(name: str) -> bool:
    """Drop the entry with this name (dash/underscore-insensitive); return
    whether one was present."""
    with mutate() as registry:
        key = match_key(name)
        kept = [p for p in registry.packages if match_key(p.name) != key]
        existed = len(kept) != len(registry.packages)
        registry.packages = kept
    return existed


def copy_changed(
    dest: Path,
    content_hash: str | None,
    *,
    skip_subtrees: frozenset[tuple[str, ...]] = frozenset(),
) -> bool:
    """True when the on-disk tree at `dest` differs from the recorded
    `content_hash` — the "local edits" signal that `ava skill update` /
    `ava plugins upgrade` / `ava mcp upgrade` must not clobber without
    `--force` (R5 design, task #1013).

    A missing recorded hash (legacy rows written before content hashing) counts
    as changed: nothing recorded means we cannot prove the copy matches what
    was last written, so the safe direction is to require an explicit force.

    For installed packages (skill/plugin/mcp) pass `entry.installed_hash` —
    converge overwrites `content_hash` on installed-plugin rows with the
    load-dir skills-copy hash, which is not the package-tree hash this
    comparison needs.
    """
    if content_hash is None:
        return True
    return tree_hash(dest, skip_subtrees=skip_subtrees) != content_hash


def enabled_skill_names() -> set[str]:
    """Names of enabled packages that may contribute skills.

    The skill scanner loads a top-level directory under `~/.ava/skills/` only
    when its name is in this set (untracked, or tracked but disabled, is not
    surfaced). Covers `type="skill"` entries (converged repo/plugin skills and
    user installs) and `type="plugin"` entries — an installed plugin's
    converged skills namespace (`~/.ava/skills/<plugin>/`) gates on the
    plugin's own registry entry rather than a duplicate skill entry.
    """
    return {p.name for p in load().packages if p.type in ("skill", "plugin") and p.enabled}


def trust_by_name() -> dict[str, TrustTier]:
    """Every tracked package's trust tier, keyed by name — one registry read for
    a caller classifying a whole skill tree at once.

    A name absent from the map is a package the registry does not track at all,
    which the skill scanner already refuses to load; a caller that still meets
    one (a runtime provider root registered by a plugin, which never passes
    through the registry) must treat it as `"unreviewed"`, the safe direction.
    """
    return {p.name: p.trust for p in load().packages}


def installed_mcp_names() -> set[str]:
    """Names of `type="mcp"` packages tracked in the registry.

    The MCP config loader loads a top-level directory under `~/.ava/mcps/` only
    when its name is in this set — so a stray dir under the load dir does not
    silently contribute a server (mirrors `enabled_skill_names`). Presence, not
    the `enabled` flag, is the gate here: runtime on/off for MCP servers lives
    in the separate per-machine `mcp_enabled.json` overlay (`ava mcp
    enable/disable`), which the loader applies uniformly to built-in, installed,
    and machine servers — so this stays a pure "is it installed" check.
    """
    return {p.name for p in load().packages if p.type == "mcp"}
