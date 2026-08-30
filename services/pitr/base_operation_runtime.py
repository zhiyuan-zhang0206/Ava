"""Shared controller implementation for scheduled and activation restore proofs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

import psutil
import psycopg

from services.pitr.base_manifest import CandidateManifest
from services.pitr.restore_manifest import ProtectedManifest
from services.pitr.restore_proof import (
    ProtectedManifestPublisher,
    RestoreSpaceBudget,
    publish_candidate_proof,
    verify_candidate_proof,
)
from services.pitr.store_factory import get_store_group
from shared.config import settings
from shared.db import direct_db_url
from shared.paths import ava_home
from shared.pg_tools import pg_tool
from shared.process_env import restricted_process_env

_EMERGENCY_FLOOR_BYTES = 4 * 1024**3


@dataclass(frozen=True)
class RestoreWorkerInput:
    candidate_json: str
    root: Path
    ack_dir: Path
    key_path: Path
    backend: str
    store_args: tuple[tuple[str, str], ...]
    budget: RestoreSpaceBudget
    live_db_url: str
    data_directory: str
    pg_ctl: Path
    pg_verifybackup: Path


def tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def input_for(candidate: CandidateManifest) -> RestoreWorkerInput:
    config = settings.physical_backup
    if not config.pitr_restore_proof_enabled:
        raise RuntimeError("restore proof cannot run while its flag is off")
    key_path = config.pitr_backup_key_file
    if key_path is None:
        raise RuntimeError("validated viewer-only restore proof key is missing")
    if config.pitr_store_backend == "gcs":
        read_credentials = config.pitr_restore_gcs_credentials_file
        if read_credentials is None:
            raise RuntimeError("validated viewer-only restore proof secrets are missing")
        store_args: tuple[tuple[str, str], ...] = (
            ("project", config.pitr_gcs_project),
            ("bucket", config.pitr_gcs_bucket),
            ("viewer_credentials", str(read_credentials)),
        )
    elif config.pitr_store_backend == "cos":
        cos_credentials = config.pitr_cos_credentials_file
        if cos_credentials is None:
            raise RuntimeError("validated COS restore-proof secrets are missing")
        store_args = (
            ("bucket", config.pitr_cos_bucket),
            ("region", config.pitr_cos_region),
            ("credentials_file", str(cos_credentials)),
            ("prefix", config.pitr_gcs_prefix),
        )
    elif config.pitr_store_backend == "baidu":
        baidu_credentials = config.pitr_baidu_credentials_file
        baidu_token = config.pitr_baidu_token_file
        if baidu_credentials is None or baidu_token is None:
            raise RuntimeError("validated Baidu restore-proof secrets are missing")
        store_args = (
            ("app_root", config.pitr_baidu_app_root),
            ("prefix", config.pitr_gcs_prefix),
            ("credentials_file", str(baidu_credentials)),
            ("token_file", str(baidu_token)),
        )
    elif config.pitr_store_backend == "oss":
        # The OSS backend: the restricted worker carries only the viewer-only
        # AccessKey pair — the reader/inventory roles alone never need the
        # uploader identity.
        read_credentials = config.pitr_oss_viewer_credentials_file
        if read_credentials is None:
            raise RuntimeError("validated viewer-only restore proof secrets are missing")
        store_args = (
            ("endpoint", config.pitr_oss_endpoint),
            ("bucket", config.pitr_oss_bucket),
            ("prefix", config.pitr_gcs_prefix),
            ("viewer_credentials_file", str(read_credentials)),
        )
    else:
        raise RuntimeError(
            f"restore proof does not know the PITR store backend {config.pitr_store_backend!r}"
        )
    root = ava_home() / "physical-backup"
    logical_peak = max(
        (item.stat().st_size for item in (ava_home() / "backups" / "db").glob("*.enc")),
        default=0,
    )
    return RestoreWorkerInput(
        candidate.to_json(),
        root,
        root / "ack",
        key_path,
        config.pitr_store_backend,
        store_args,
        RestoreSpaceBudget(config.pitr_spool_hard_bytes, logical_peak, _EMERGENCY_FLOOR_BYTES),
        direct_db_url(),
        live_data_directory(),
        pg_tool("pg_ctl"),
        pg_tool("pg_verifybackup"),
    )


def live_data_directory() -> str:
    """The live instance's PGDATA, certified on the admin connection.

    The restore worker's live-identity probe runs on the runtime role
    (AVA_DB_URL), which must stay free of settings-read privileges:
    PG 17 gates `current_setting('data_directory')` behind
    pg_read_all_settings, and the 2026-08-30 activation died on exactly that
    grant gap. The controller reads the value once on the admin connection
    and hands it to the worker in its request instead."""
    from cli.commands._cluster_instance import pg_admin_url
    from shared.cluster import get_record, record_postgres_port

    record = get_record(ava_home())
    if record is None:
        raise RuntimeError("cluster registry record is missing")
    with psycopg.connect(pg_admin_url(record_postgres_port(record))) as conn:
        row = conn.execute("SELECT current_setting('data_directory')").fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL omitted its data directory")
    return str(row[0])


def _request(inputs: RestoreWorkerInput) -> dict[str, object]:
    return {
        "candidate_json": inputs.candidate_json,
        "root": str(inputs.root),
        "ack_dir": str(inputs.ack_dir),
        "key_path": str(inputs.key_path),
        "backend": inputs.backend,
        "store_args": dict(inputs.store_args),
        "budget": {
            "spool_and_pg_wal_reserve": inputs.budget.spool_and_pg_wal_reserve,
            "logical_backup_peak": inputs.budget.logical_backup_peak,
            "emergency_floor": inputs.budget.emergency_floor,
        },
        "live_db_url": inputs.live_db_url,
        "data_directory": inputs.data_directory,
        "pg_ctl": str(inputs.pg_ctl),
        "pg_verifybackup": str(inputs.pg_verifybackup),
    }


async def run_restore(candidate: CandidateManifest) -> dict[str, str]:
    return await run_restore_input(input_for(candidate))


async def run_restore_input(inputs: RestoreWorkerInput) -> dict[str, str]:
    control_root = inputs.root / "restore-control"
    control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = Path(tempfile.mkdtemp(prefix=".proof-", dir=control_root))
    request, result = work / "request.json", work / "result.json"
    acknowledgement = result.with_suffix(".ack")
    request.write_text(json.dumps(_request(inputs), sort_keys=True, separators=(",", ":")))
    request.chmod(0o600)
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "services.pitr.restore_worker", str(request), str(result)],
        cwd=Path(__file__).resolve().parents[2],
        env=restricted_process_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
        text=True,
    )
    leader_created_at = psutil.Process(process.pid).create_time()
    try:
        while not result.is_file():
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr is not None else ""
                return restore_result(process.returncode, stderr, result)
            await asyncio.sleep(0.25)
        if [member for member in group_members(process.pid) if member.pid != process.pid]:
            reap_restore_group(process, leader_created_at)
            _reject_restore_descendants()
        acknowledgement.write_text("accepted")
        acknowledgement.chmod(0o600)
        while process.poll() is None:
            await asyncio.sleep(0.05)
        if group_members(process.pid):
            _raise_surviving_restore_group()
        stderr = process.stderr.read() if process.stderr is not None else ""
        return restore_result(process.returncode, stderr, result)
    except BaseException:
        if process.poll() is None or group_members(process.pid):
            reap_restore_group(process, leader_created_at)
        raise
    finally:
        if process.stderr is not None:
            process.stderr.close()
        shutil.rmtree(work, ignore_errors=True)


def reap_restore_group(process: subprocess.Popen[str], leader_created_at: float) -> None:
    if process.pid == os.getpgrp():
        raise RuntimeError("refusing to signal the controller process group")
    try:
        leader = psutil.Process(process.pid)
        if abs(leader.create_time() - leader_created_at) >= 0.01:
            raise RuntimeError("restricted restore worker PID identity changed")
    except psutil.NoSuchProcess as exc:
        if group_members(process.pid):
            raise RuntimeError(
                "restricted restore descendants outlived their verifiable leader"
            ) from exc
        process.wait(timeout=1)
        return
    deadline = time.monotonic() + 20
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    grace = min(deadline, time.monotonic() + 5)
    while group_members(process.pid) and time.monotonic() < grace:
        time.sleep(0.1)
    while group_members(process.pid) and time.monotonic() < deadline:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        time.sleep(0.1)
    process.wait(timeout=max(0.1, deadline - time.monotonic()))
    if group_members(process.pid):
        raise RuntimeError("restricted restore worker process group could not be emptied")


def group_members(pgid: int) -> list[psutil.Process]:
    members: list[psutil.Process] = []
    for process in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(process.pid) == pgid:
                members.append(process)
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
            continue
    return members


def _reject_restore_descendants() -> NoReturn:
    raise RuntimeError("restricted restore worker left live descendants")


def _raise_surviving_restore_group() -> NoReturn:
    raise RuntimeError("restricted restore worker group survived its owned leader")


def restore_result(returncode: int, stderr: str, result: Path) -> dict[str, str]:
    if returncode != 0:
        raise RuntimeError(f"restricted restore worker exited {returncode}: {stderr}")
    loaded: object = json.loads(result.read_text())
    if not isinstance(loaded, dict):
        raise TypeError("restricted restore worker result must be an object")
    raw = cast(dict[str, object], loaded)
    if set(raw) != {"chain_id", "candidate_sha256", "pending_sha256"}:
        raise RuntimeError("restricted restore worker returned an invalid result")
    return {key: str(value) for key, value in raw.items()}


def publish(
    candidate: CandidateManifest,
    outcome: dict[str, str],
    *,
    require_ownership: Callable[[], None] = lambda: None,
) -> None:
    config = settings.physical_backup
    root = ava_home() / "physical-backup"
    path = root / "base-manifests" / f"{candidate.chain_id}.candidate.json"
    authoritative = CandidateManifest.from_json(path.read_text())
    candidate_digest = hashlib.sha256(authoritative.to_json().encode()).hexdigest()
    pending = root / "protected-pending" / f"{authoritative.chain_id}.json"
    if (
        authoritative != candidate
        or outcome.get("chain_id") != authoritative.chain_id
        or outcome.get("candidate_sha256") != candidate_digest
        or outcome.get("pending_sha256") != hashlib.sha256(pending.read_bytes()).hexdigest()
    ):
        raise RuntimeError("restricted restore outcome differs from authoritative evidence")
    verified, publisher = verify_then_construct_publisher(
        candidate=authoritative,
        root=root,
        ack_dir=root / "ack",
    )
    publish_candidate_proof(
        candidate=authoritative,
        root=root,
        prefix=config.pitr_gcs_prefix,
        verified=verified,
        publisher=publisher,
        require_ownership=require_ownership,
    )


publish_restore = publish


def verify_then_construct_publisher(
    *,
    candidate: CandidateManifest,
    root: Path,
    ack_dir: Path,
) -> tuple[ProtectedManifest, ProtectedManifestPublisher]:
    verified = verify_candidate_proof(candidate=candidate, root=root, ack_dir=ack_dir)
    publisher = get_store_group().protected_manifest_publisher()
    return verified, publisher
