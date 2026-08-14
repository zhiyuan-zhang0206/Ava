"""Cluster registry — the host-level `~/.ava/clusters.json` record store.

The home-keyed registry: `ClusterRecord` (the on-disk record shape, carrying
the compat name/db_name a box-shared pre-cutover reader needs), load/save/delete
under `registry_lock()`, and `migrate_registry_keys()` — the converge-time repair
that rewrites a buggy home-keyed file back to the migration-window name-keyed
form. Cluster identity IS the home path; the registry is the box-level map from
home to record. See the package docstring for the identity model.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Generator
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from shared import cluster
from shared.config import settings
from shared.platform import file_lock


@dataclass(frozen=True)
class ClusterRecord:
    ports: cluster.ClusterPorts
    gateway_home: str
    created_at: str
    # Backward-compat passthrough (path-only migration window). The host registry
    # `~/.ava/clusters.json` is box-level SHARED, so a pre-cutover reader on the
    # same box (prod main, preview) still loads it — and that code's ClusterRecord
    # REQUIRES `name` + `db_name` and looks records up BY name. So the on-disk file
    # stays name-keyed and every record carries both fields for the whole window.
    # Path-only code never reads them for logic (identity is the home path / the
    # .env URL); they are synthesized for a nameless birth (`name` = home slug,
    # `db_name` = the fixed data-plane identity) and are dropped only by a future
    # CONTRACT release, once no pre-cutover reader shares this box's registry.
    name: str = ""
    db_name: str = ""

    def __post_init__(self) -> None:
        # Frozen dataclass: fill the compat fields in place when a path-only birth
        # constructed the record without them, so load/save round-trips are stable.
        if not self.name and self.gateway_home:
            object.__setattr__(self, "name", cluster.home_slug(Path(self.gateway_home)))
        if not self.db_name:
            object.__setattr__(self, "db_name", cluster.DATA_PLANE_IDENTITY)


def registry_path() -> Path:
    return Path(settings.general.cluster_registry).expanduser()


def load_registry() -> dict[str, ClusterRecord]:
    """The registry, keyed IN MEMORY by gateway_home path. Tolerates both on-disk
    shapes: a file keyed by cluster NAME (the pre-cutover / migration-window form)
    is re-keyed on load from each record's own `gateway_home`, and truly-retired
    fields the dataclass no longer declares (`redis_db_index` / `redis_prefix`)
    are dropped. The compat `name` / `db_name` ARE kept on the record (a
    box-shared pre-cutover reader still needs them on disk — see ClusterRecord).
    So the in-memory key is always the home; the on-disk key stays the name."""
    p = cluster.registry_path()
    if not p.exists():
        return {}
    raw = json.loads(p.read_text())
    known = {f.name for f in fields(ClusterRecord)}
    out: dict[str, ClusterRecord] = {}
    origin: dict[str, str] = {}
    for k, v in raw.items():
        rec = ClusterRecord(**{kk: vv for kk, vv in v.items() if kk in known})
        if not rec.gateway_home:
            raise RuntimeError(
                f"clusters.json record {k!r} has no gateway_home — the home path IS the "
                f"cluster identity; fix or remove the record in {p}"
            )
        if rec.gateway_home in out:
            # Two records claiming one home would silently last-win here and the
            # converge migration would then PERSIST the loss (freeing a port block
            # that may still be in use). Refuse; the operator resolves by hand.
            raise RuntimeError(
                f"clusters.json has two records claiming home {rec.gateway_home!r} "
                f"(keys {origin[rec.gateway_home]!r} and {k!r}) — resolve the duplicate "
                f"in {p} before proceeding"
            )
        out[rec.gateway_home] = rec
        origin[rec.gateway_home] = k
    return out


def _registry_disk_form(reg: dict[str, ClusterRecord]) -> dict[str, dict[str, Any]]:
    """The on-disk JSON: keyed by NAME (not the in-memory home key), each record
    carrying the compat name/db_name via asdict. Name-keyed because a box-shared
    pre-cutover reader looks the shared registry up by name; home-keying + field
    drop is the future CONTRACT step (see ClusterRecord). `name` is unique per
    record (a preserved legacy name, or a home-slug synthesized in __post_init__).

    The comprehension below deliberately REFUSES a duplicate name (F-s4-8):
    the on-disk form is name-keyed, so a collision would silently overwrite
    one record and free its port block — exactly the identifier-collision the
    "identity IS the path" concept exists to rule out. Two same-name records
    are legal in memory (keyed by home); the operator resolves the name clash
    before the next save."""
    out: dict[str, dict[str, Any]] = {}
    for rec in reg.values():
        if rec.name in out:
            raise RuntimeError(
                f"registry records for homes {out[rec.name]['gateway_home']!r} and "
                f"{rec.gateway_home!r} share the compat name {rec.name!r} — the on-disk "
                f"registry is name-keyed during the migration window and one would "
                f"silently overwrite the other; rename or remove a record in "
                f"{cluster.registry_path()}"
            )
        out[rec.name] = asdict(rec)
    return out


def _dump_registry(reg: dict[str, ClusterRecord]) -> None:
    p = cluster.registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(_registry_disk_form(reg), indent=2)
    # atomic write so a concurrent reader never sees a half-written file
    tmp = p.with_suffix(".tmp")
    tmp.write_text(data)
    tmp.replace(p)


def save_record(rec: ClusterRecord) -> None:
    """Insert/update a cluster's record — self-serializing: the registry
    read-modify-write runs under registry_lock() internally, so a caller that
    does NOT hold the lock cannot clobber a concurrent birth (audit 2026-08-08
    P2: _converge_gate._ensure_app_port called save_record without the lock,
    and a lost update resurrected/dropped records mid-race). Callers that
    already hold the lock (a birth's allocate+save critical section) use
    save_record_locked."""
    with registry_lock():
        save_record_locked(rec)


def save_record_locked(rec: ClusterRecord) -> None:
    """save_record for a caller that already holds registry_lock()."""
    reg = load_registry()
    reg[rec.gateway_home] = rec
    _dump_registry(reg)


def delete_record(home: Path) -> bool:
    """Remove a cluster from the registry (frees its port block for reuse).
    Returns True if a record was removed. Self-serializing — see save_record."""
    with registry_lock():
        return delete_record_locked(home)


def delete_record_locked(home: Path) -> bool:
    """delete_record for a caller that already holds registry_lock()."""
    reg = load_registry()
    key = str(Path(home).expanduser())
    if key not in reg:
        return False
    del reg[key]
    _dump_registry(reg)
    return True


def get_record(home: Path) -> ClusterRecord | None:
    return load_registry().get(str(Path(home).expanduser()))


def migrate_registry_keys() -> bool:
    """Idempotently normalize `clusters.json` to the migration-window form:
    name-keyed, every record carrying the compat name/db_name a box-shared
    pre-cutover reader requires, retired unknown fields (redis_db_index /
    redis_prefix) dropped. `load_registry` re-keys by home on read, so path-only
    code never needs this — its real job is to REPAIR a file a buggy path-only
    build already rewrote to home keys WITHOUT the compat fields (which crashes a
    pre-cutover reader with `TypeError: missing name/db_name`), backfilling the
    synthesized fields. Home-keying + dropping name/db_name is the future
    CONTRACT step. Returns True when the file was rewritten. Run by converge on
    every start."""
    p = cluster.registry_path()
    if not p.exists():
        return False
    with registry_lock():
        raw = json.loads(p.read_text())
        reg = load_registry()
        canonical = _registry_disk_form(reg)
        if raw == canonical:
            return False
        _dump_registry(reg)
    return True


@contextlib.contextmanager
def registry_lock() -> Generator[None]:
    """Host-level advisory file lock serializing registry read-modify-write.

    Cluster birth (install) does load_registry -> allocate ports -> save_record
    as one critical section; without a lock two concurrent births both read the
    same registry, allocate the same port block, and the second save_record
    clobbers the first. Hold this across the whole allocate+save.
    """
    lock_path = cluster.registry_path().with_suffix(".lock")
    # Cross-platform advisory lock (fcntl on POSIX, msvcrt on Windows) — see
    # shared.platform.file_lock. Serializes the registry read-modify-write.
    with file_lock(lock_path):
        yield
