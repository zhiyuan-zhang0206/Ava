"""Shared test scaffolding for the WAL-G package: a sandbox home and a real Postgres.

Not a test module: pytest never collects this file. The sandbox installs
`fake_walg.sh` where the pinned binary lives, writes a valid configuration and
key, and points the settings and the binary pin at them, so production code runs
unchanged. The home path deliberately holds a space and a `%p`: the archive
command must survive both (an unescaped `%p` in a path would be expanded by
Postgres into a segment path).
"""

from __future__ import annotations

import json
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.cluster.dataplane import walg_binary
from base.cluster.dataplane.pg_tools import pg_shm_args, pg_start_env, pg_tool, pg_tz_args
from base.config import settings
from services.gateway_side.walg.archive import archive_pg_args

FAKE_WALG = Path(__file__).with_name("fake_walg.sh")
FIXTURES = Path(__file__).with_name("fixtures")

# Values that must never appear in argv, postgresql.conf, a log line or an error.
ACCESS_KEY_ID = "LTAI-test-access-key-id"
ACCESS_KEY_SECRET = "test-access-key-secret-0123456789"  # noqa: S105 — a fixture value
KEY_HEX = "ab" * 32
PREFIX = "oss://ava-backups/ava-walg/test-home/pg17/gen1/"
SECRETS = (ACCESS_KEY_ID, ACCESS_KEY_SECRET, KEY_HEX)


@dataclass
class Sandbox:
    home: Path
    store_dir: Path
    config_file: Path
    key_file: Path

    def config(self) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(self.config_file.read_text())
        return payload

    def write_config(self, payload: dict[str, Any]) -> None:
        self.config_file.write_text(json.dumps(payload))
        self.config_file.chmod(0o600)

    def set_mode(self, mode: str) -> None:
        (self.store_dir / "mode").write_text(mode)

    def put(self, name: str, text: str) -> None:
        """Script the fake: write one of the files it reads from its directory."""
        (self.store_dir / name).write_text(text)

    def fail(self, *commands: str) -> None:
        """Make these wal-g commands exit 1 (the rest keep working)."""
        self.put("fail-commands", " ".join(commands))

    def env_log(self) -> list[str]:
        log = self.store_dir / "env.log"
        return log.read_text().splitlines() if log.exists() else []

    def verify_env_log(self) -> list[str]:
        log = self.store_dir / "verify-env.log"
        return log.read_text().splitlines() if log.exists() else []

    def calls(self) -> list[str]:
        log = self.store_dir / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def stored(self) -> list[str]:
        root = self.store_dir / "store"
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def fixture_text(name: str) -> str:
    """A file of real WAL-G output, kept in `tests/fixtures/` (see the test that uses it)."""
    return (FIXTURES / name).read_text()


def valid_config(key_file: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "WALG_OSS_PREFIX": PREFIX,
        "OSS_ACCESS_KEY_ID": ACCESS_KEY_ID,
        "OSS_ACCESS_KEY_SECRET": ACCESS_KEY_SECRET,
        "OSS_ENDPOINT": "https://oss-cn-shanghai.aliyuncs.com",
        "OSS_REGION": "cn-shanghai",
        "WALG_LIBSODIUM_KEY_PATH": str(key_file),
        "WALG_LIBSODIUM_KEY_TRANSFORM": "hex",
        "WALG_PREVENT_WAL_OVERWRITE": "true",
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


def make_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True
) -> Sandbox:
    home = tmp_path / "ava home%p"
    store_dir = home / "secrets" / "walg"
    store_dir.mkdir(parents=True)
    key_file = store_dir / "libsodium.key"
    key_file.write_text(KEY_HEX + "\n")
    key_file.chmod(0o600)
    config_file = store_dir / "walg.json"

    binary = home / "runtime" / "walg" / "wal-g"
    binary.parent.mkdir(parents=True)
    shutil.copy(FAKE_WALG, binary)
    binary.chmod(0o755)

    monkeypatch.setenv("AVA_HOME", str(home))
    # The fake is not the pinned build; everything else (path, argv, config) is real.
    monkeypatch.setattr(walg_binary, "installed_problem", lambda: None)
    sandbox = Sandbox(home, store_dir, config_file, key_file)
    sandbox.write_config(valid_config(key_file))
    monkeypatch.setattr(settings.walg, "walg_config_file", config_file if enabled else None)
    return sandbox


# ── a real Postgres, launched the way the cluster launches it ────────────────


@dataclass
class PgInstance:
    root: Path
    data: Path
    port: int

    def connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(
            host=str(self.root), port=self.port, user="ava", dbname="postgres", autocommit=True
        )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_ready(instance: PgInstance, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log = (instance.root / "pg.log").read_text()
            raise RuntimeError(f"postgres exited {process.returncode} during start:\n{log[-2000:]}")
        with suppress(psycopg.OperationalError):
            instance.connect().close()
            return
        time.sleep(0.2)
    raise RuntimeError("postgres did not accept connections in 60s")


@contextmanager
def archiving_postgres(*extra_args: str) -> Generator[PgInstance]:
    """initdb + launch a Postgres with `archive_pg_args()` on its command line.

    The socket directory is short (`/tmp`), the data is disposable (fsync off), and
    shutdown is immediate: a test never waits for a hung archive command.
    """
    root = Path(tempfile.mkdtemp(prefix="ava-walgpg-", dir="/tmp"))
    data = root / "data"
    instance = PgInstance(root=root, data=data, port=_free_port())
    process: subprocess.Popen[bytes] | None = None
    try:
        subprocess.run(  # noqa: S603 — argv is the resolved initdb path + static flags
            [
                str(pg_tool("initdb")),
                "-D",
                str(data),
                "-U",
                "ava",
                "-A",
                "trust",
                "--no-sync",
                "--encoding=UTF8",
                "--locale=C",
            ],
            check=True,
            capture_output=True,
        )
        argv = [
            str(pg_tool("postgres")),
            "-D",
            str(data),
            "-p",
            str(instance.port),
            "-c",
            "listen_addresses=",
            "-c",
            f"unix_socket_directories={root}",
            "-c",
            "fsync=off",
            "-c",
            "full_page_writes=off",
            "-c",
            "synchronous_commit=off",
            *shlex.split(pg_tz_args()),
            *shlex.split(pg_shm_args()),
            *archive_pg_args(),
            *extra_args,
        ]
        with (root / "pg.log").open("wb") as log:
            process = subprocess.Popen(  # noqa: S603 — resolved postgres path + static flags
                argv, stdout=log, stderr=log, env=pg_start_env(), start_new_session=True
            )
        _wait_ready(instance, process)
        yield instance
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGQUIT)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=30)
            if process.poll() is None:
                process.kill()
                process.wait()
        shutil.rmtree(root, ignore_errors=True)


def wait_for(predicate: Any, *, seconds: float = 60, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out after {seconds:g}s waiting for {what}")


def write_some_wal_and_switch(conn: psycopg.Connection[Any]) -> None:
    """Make sure the current segment has content, then close it so it becomes archivable."""
    conn.execute("CREATE TABLE IF NOT EXISTS walg_probe (n int)")
    conn.execute("INSERT INTO walg_probe SELECT generate_series(1, 100)")
    conn.execute("SELECT pg_switch_wal()")


def archive_current_segment(conn: psycopg.Connection[Any], sandbox: Sandbox) -> str:
    """Close the current WAL segment and wait until the fake store holds it; returns its name.

    A segment that has had no WAL since it started is not switched, so one record is
    emitted first.
    """
    conn.execute("SELECT pg_logical_emit_message(false, 'walg-test', 'x')")
    row = conn.execute("SELECT pg_walfile_name(pg_current_wal_lsn())").fetchone()
    assert row is not None
    segment = str(row[0])
    conn.execute("SELECT pg_switch_wal()")
    wait_for(lambda: segment in sandbox.stored(), what=f"{segment} in the store")
    return segment


def take_basebackup(sandbox: Sandbox, pg: PgInstance, name: str) -> str:
    """A base backup of `pg` as the fake's `backup-fetch` serves it (no WAL inside, like WAL-G's)."""
    destination = sandbox.store_dir / "basebackups" / name
    subprocess.run(  # noqa: S603 — the resolved pg_basebackup with static flags
        [
            str(pg_tool("pg_basebackup")),
            "-D",
            str(destination),
            "-h",
            str(pg.root),
            "-p",
            str(pg.port),
            "-U",
            "ava",
            "-X",
            "none",
            "--checkpoint=fast",
            "--no-sync",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return name
