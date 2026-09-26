"""Cluster registry — the host-level `~/.ava/clusters.json` record store.

The home-keyed registry: `ClusterRecord` (the on-disk record shape), load/
save/delete under `registry_lock()`. Cluster identity IS the home path; the
registry is the box-level map from home to record. See the package docstring
for the identity model.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Generator
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from shared import cluster
from shared.platform import file_lock


@dataclass(frozen=True)
class ClusterRecord:
    ports: cluster.ClusterPorts
    gateway_home: str
    created_at: str
    # The host this cluster's data-plane URLs are DERIVED at (install birth);
    # empty = loopback (127.0.0.1), the single-box posture. Stored on the record
    # because derivation happens at birth, before the home's `.env` exists; the
    # birth path snapshots it from `settings.data_plane.data_plane_host`
    # (AVA_DATA_PLANE_HOST) — see `cli.commands.cluster_lifecycle._ensure_record`
    # and `shared.cluster.derive.per_cluster_base_urls`. External data plane:
    # Task #1752.
    data_plane_host: str = ""


def registry_path() -> Path:
    from shared.config import settings

    return Path(settings.general.cluster_registry).expanduser()


def load_registry(*, path: Path | None = None) -> dict[str, ClusterRecord]:
    """The registry, keyed IN MEMORY by gateway_home path. The record's own
    `gateway_home` is the only identity — a file key written by an older
    (name-keyed) build still loads because every row is re-keyed from its
    `gateway_home` — and truly-retired fields the dataclass no longer declares
    (`redis_db_index` / `redis_prefix`) are dropped."""
    p = path if path is not None else cluster.registry_path()
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
    """The on-disk JSON: keyed by home path — the record identity — one entry
    per record (`asdict` over the declared fields)."""
    return {home: asdict(rec) for home, rec in reg.items()}


def _dump_registry(reg: dict[str, ClusterRecord], *, path: Path | None = None) -> None:
    p = path if path is not None else cluster.registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(_registry_disk_form(reg), indent=2)
    from shared.atomic_io import write_text_atomic

    write_text_atomic(p, data, mode=0o600, sync_parent=True)


def save_record(rec: ClusterRecord) -> None:
    """Insert/update a cluster's record — self-serializing: the registry
    read-modify-write runs under registry_lock() internally, so a caller that
    does NOT hold the lock cannot clobber a concurrent birth (audit 2026-08-08
    A port-block backfill called save_record without the lock,
    and a lost update resurrected/dropped records mid-race). Callers that
    already hold the lock (a birth's allocate+save critical section) use
    save_record_locked."""
    with registry_lock():
        save_record_locked(rec)


def save_record_locked(rec: ClusterRecord, *, path: Path | None = None) -> None:
    """save_record for a caller that already holds registry_lock()."""
    reg = load_registry(path=path)
    reg[rec.gateway_home] = rec
    _dump_registry(reg, path=path)


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


@contextlib.contextmanager
def registry_lock(*, path: Path | None = None, timeout_s: float = 30) -> Generator[None]:
    """Host-level advisory file lock serializing registry read-modify-write.

    Cluster start does load_registry -> allocate ports -> save_record
    as one critical section; without a lock two concurrent births both read the
    same registry, allocate the same port block, and the second save_record
    clobbers the first. Hold this across the whole allocate+save.
    """
    lock_path = (path if path is not None else cluster.registry_path()).with_suffix(".lock")
    # Cross-platform advisory lock (fcntl on POSIX, msvcrt on Windows) — see
    # shared.platform.file_lock. Serializes the registry read-modify-write.
    with file_lock(lock_path, timeout_s=timeout_s):
        yield
