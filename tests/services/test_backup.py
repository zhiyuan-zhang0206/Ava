"""services/backup.py — due-ness, prune, foreign-file safety, failure cleanup,
and a real pg_dump round-trip against the session's provisioned Postgres.

Every clock here is pinned: the module decides *when* in cluster time
(`AVA_TIMEZONE`) and *names* dumps in UTC, so a test that let either fall back
to the host's timezone would pass or fail by which machine ran it.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from services import backup
from services.pitr import store_factory
from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.object_store import RemoteObjectAck
from shared.config import settings
from shared.platform import LockTimeoutError

_CLUSTER_TZ = "America/Los_Angeles"
_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def bdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(backup, "backup_dir", lambda: tmp_path)
    monkeypatch.setattr(settings.general, "timezone", _CLUSTER_TZ)
    return tmp_path


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """A cluster wall-clock instant — the clock is_due() reads."""
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(_CLUSTER_TZ))


def _touch(directory: Path, name: str) -> Path:
    p = directory / name
    p.write_bytes(b"x")
    return p


def _disable_offsite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the off-site leg degrade ("store unavailable") in tests that only
    exercise the local pipeline — the same failure mode a cluster without a
    configured backup store hits."""

    def _no_store_group() -> Any:
        raise RuntimeError("no backup store configured")

    monkeypatch.setattr(store_factory, "get_store_group", _no_store_group)


def _spawn_backup_lock_holder(
    ava_home: Path, ready: Path, hold_s: float
) -> subprocess.Popen[bytes]:
    """A separate interpreter that takes `backup_lock`, signals, and holds it."""
    code = textwrap.dedent(f"""
        import sys
        import time
        from pathlib import Path

        sys.path.insert(0, {str(_REPO)!r})
        from services.backup import backup_lock
        from shared.config import settings

        settings.general.ava_home = Path({str(ava_home)!r})
        with backup_lock(timeout_s=60):
            Path({str(ready)!r}).write_text("1", encoding="utf-8")
            time.sleep({hold_s})
    """)
    env = dict(os.environ)
    env["AVA_HOME"] = str(ava_home)
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )


def _await_backup_lock_holder(ready: Path, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert proc.poll() is None, "the holder exited before taking the backup lock"
        assert time.monotonic() < deadline, "the holder never took the backup lock"
        time.sleep(0.02)


def test_not_due_before_backup_hour(bdir: Path) -> None:
    assert not backup.is_due(_dt(2026, 6, 10, backup.BACKUP_HOUR - 1, 59))


def test_due_at_hour_with_no_dumps(bdir: Path) -> None:
    assert backup.is_due(_dt(2026, 6, 10, backup.BACKUP_HOUR, 0))


def test_not_due_again_after_todays_dump(bdir: Path) -> None:
    _touch(bdir, "ava-20260610-030001.dump")
    assert not backup.is_due(_dt(2026, 6, 10, 9, 0))


def test_due_again_the_next_day(bdir: Path) -> None:
    _touch(bdir, "ava-20260609-030001.dump")
    assert backup.is_due(_dt(2026, 6, 10, backup.BACKUP_HOUR, 0))


def test_catchup_after_downtime(bdir: Path) -> None:
    """Host down at 03:00 -> the first tick later that day is still due."""
    _touch(bdir, "ava-20260609-030001.dump")
    assert backup.is_due(_dt(2026, 6, 10, 14, 30))


# ─── the clock the schedule is read on ───


def test_naive_now_is_rejected(bdir: Path) -> None:
    """A naive datetime is refused rather than read as host-local — reading the
    host clock is exactly the failure the cluster-time pin removes."""
    _ = bdir
    with pytest.raises(ValueError, match="TZ-aware"):
        backup.is_due(datetime(2026, 6, 10, 3, 0))  # noqa: DTZ001 — the value under test


def test_day_boundary_is_cluster_time_not_host_time(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One instant, several host clocks, one verdict.

    The regression: due-ness used to be computed on the host's wall clock, so
    carrying a laptop from Asia/Shanghai to US/Pacific put the newest dump's
    local date *ahead* of the new local date. `is_due` returned False and the
    daily backup was skipped silently — no error, no log, just no dump.
    """
    monkeypatch.setattr(settings.general, "timezone", "Asia/Shanghai")
    _touch(bdir, "ava-20260609T190000Z.dump")  # 03:00 Jun-10 Shanghai

    today = datetime(2026, 6, 9, 20, 0, tzinfo=UTC)  # 04:00 Jun-10 Shanghai
    for host_tz in ("Asia/Shanghai", "America/Los_Angeles", "Pacific/Kiritimati"):
        assert not backup.is_due(today.astimezone(ZoneInfo(host_tz))), host_tz

    tomorrow = datetime(2026, 6, 10, 20, 0, tzinfo=UTC)  # 04:00 Jun-11 Shanghai
    for host_tz in ("Asia/Shanghai", "America/Los_Angeles", "Pacific/Kiritimati"):
        assert backup.is_due(tomorrow.astimezone(ZoneInfo(host_tz))), host_tz


def test_backup_hour_is_cluster_time(bdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BACKUP_HOUR is 03:00 on the cluster's clock, whatever the host's says."""
    monkeypatch.setattr(settings.general, "timezone", "Asia/Shanghai")
    assert not backup.is_due(datetime(2026, 6, 9, 18, 59, tzinfo=UTC))  # 02:59 Shanghai
    assert backup.is_due(datetime(2026, 6, 9, 19, 0, tzinfo=UTC))  # 03:00 Shanghai


# ─── the clock dumps are named on ───


def test_dump_name_is_utc_stamped(bdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The filename stamp is UTC with an explicit `Z`, not cluster wall clock:
    the name has to identify one instant for prune's ordering to be a total
    order."""
    monkeypatch.setattr(settings.general, "timezone", "Asia/Shanghai")

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **_kw: object) -> _Ok:
        if cmd[0].endswith("pg_dump"):
            Path(cmd[cmd.index("--file") + 1]).write_bytes(b"x")
        else:
            Path(cmd[cmd.index("-out") + 1]).write_bytes(b"encrypted dump")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)
    path = backup.run_backup(
        datetime(2026, 6, 10, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai")), db_url="dbname=ava"
    )
    assert path.name == "ava-20260609T190000Z.dump.enc"


def test_pre_update_backup_can_defer_offsite_publish(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prepare-phase snapshot retains a verified local artifact without a network publish."""
    published: list[Path] = []

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **_kw: object) -> _Ok:
        if cmd[0].endswith("pg_dump"):
            Path(cmd[cmd.index("--file") + 1]).write_bytes(b"dump")
        else:
            Path(cmd[cmd.index("-out") + 1]).write_bytes(b"encrypted")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)
    monkeypatch.setattr(backup, "_publish_offsite", published.append)

    artifact = backup.run_backup(
        _dt(2026, 6, 10, 3, 0), db_url="dbname=ava", pre_update=True, publish=False
    )

    assert artifact.exists()
    assert published == []


def test_publish_offsite_module_entry_publishes_the_named_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The detached uploader entry delegates one existing artifact to idempotent publishing."""
    artifact = tmp_path / "ava-pre-update.dump.enc"
    artifact.write_bytes(b"encrypted")
    published: list[Path] = []
    monkeypatch.setattr(backup, "_publish_offsite", published.append)

    assert backup._main(["--publish-offsite", str(artifact)]) == 0

    assert published == [artifact]


def test_db_size_breakdown_real_db(db_conn: Any) -> None:
    """The composition query itself is pinned against a real throwaway DB: a
    fresh DB with no checkpoint tables reads 0 instead of failing (the
    to_regclass path). The frozen `events` archive is gone since the task
    #1281/#1823 cleanup, so it is no longer part of the composition."""
    _ = db_conn  # dependency: the session cluster is up
    base_url, _name = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"ava_test_backup_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        line = backup._db_size_breakdown(url)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
    assert line.startswith("db=") and "checkpoint=" in line and "rest=" in line
    assert line != "unavailable"
    # A fresh DB has no checkpoint tables.
    assert "checkpoint=0MiB" in line


def test_db_size_breakdown_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backup log line reports the DB composition that dominates dump
    time: total, the checkpoint tables, and the rest. Best-effort: a failed
    sample degrades to "unavailable", never fails the backup."""

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, _sql: str) -> _FakeConn:
            return self

        def fetchone(self) -> tuple[int, ...]:
            # db, blobs, checkpoints, writes
            return (4_240_000_000, 1_240_000_000, 130_000_000, 30_000_000)

    def _fake_connect(**_: object) -> _FakeConn:
        return _FakeConn()

    monkeypatch.setattr(backup, "connect", _fake_connect)
    line = backup._db_size_breakdown()
    assert line == "db=4044MiB checkpoint=1335MiB rest=2708MiB"

    def _boom(**_: object) -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr(backup, "connect", _boom)
    assert backup._db_size_breakdown() == "unavailable"


def test_run_backup_repairs_storage_permissions(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup directory and completed dump are owner-only despite umask drift."""
    bdir.chmod(0o755)

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **kw: object) -> _Ok:
        if cmd[0].endswith("pg_dump"):
            partial = Path(cmd[cmd.index("--file") + 1])
            partial.write_bytes(b"dump")
            partial.chmod(0o644)
        elif cmd[0].endswith("openssl"):
            Path(cmd[cmd.index("-out") + 1]).write_bytes(b"encrypted")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)
    target = backup.run_backup(_dt(2026, 6, 10, 3, 0), db_url="dbname=ava")

    assert bdir.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600


def test_prune_order_survives_the_dst_fold(bdir: Path) -> None:
    """The two dumps in the DST fall-back hour order by instant, not wall clock.

    On 2026-11-01 in America/Los_Angeles, 01:30 happens twice — once PDT, once
    PST an hour later. Under the old local naming both dumps were literally
    called `ava-20261101-013000.dump` (the second silently replacing the
    first), and re-parsing that name naive-then-local left which one counted as
    "oldest" up to whichever offset the parse picked. Prune deletes by this
    ordering, so the ambiguity decided which backup was destroyed.
    """
    pdt = _touch(bdir, "ava-20261101T083000Z.dump")  # 01:30 PDT
    pst = _touch(bdir, "ava-20261101T093000Z.dump")  # 01:30 PST, one hour later
    assert [p for _ts, p in backup._managed_dumps(bdir)] == [pdt, pst]

    for i in range(2, backup.BACKUP_KEEP + 1):
        _touch(bdir, f"ava-202611{i:02d}T090000Z.dump")
    assert backup._prune(bdir) == [pdt]  # the earlier half of the fold, deterministically
    assert pst.exists()


def test_legacy_named_dumps_stay_managed(bdir: Path) -> None:
    """Pre-cutover names (host wall clock, no offset) still count for due-ness
    and still prune, so a cutover does not strand a week of dumps as
    permanently-ignored foreign files. They sort against UTC-stamped names by
    reading their stamp in cluster time."""
    legacy = _touch(bdir, "ava-20261101-013000.dump")  # 01:30 cluster time
    modern = _touch(bdir, "ava-20261101T220000Z.dump")  # 14:00 cluster time, same day
    assert [p for _ts, p in backup._managed_dumps(bdir)] == [legacy, modern]
    assert not backup.is_due(_dt(2026, 11, 1, 23, 0))

    for i in range(2, backup.BACKUP_KEEP + 1):
        _touch(bdir, f"ava-202611{i:02d}-030000.dump")
    assert backup._prune(bdir) == [legacy]


def test_foreign_files_ignored(bdir: Path) -> None:
    """Hand-made dumps / stray files are neither counted for due-ness nor pruned."""
    foreign = [
        _touch(bdir, "manual.dump"),
        _touch(bdir, "ava-before-migration.dump"),
        _touch(bdir, "notes.txt"),
    ]
    assert backup.is_due(_dt(2026, 6, 10, 9, 0))
    assert backup._prune(bdir) == []
    assert all(p.exists() for p in foreign)


def test_prune_keeps_newest(bdir: Path) -> None:
    names = [f"ava-202606{i:02d}-030000.dump" for i in range(1, 11)]  # 10 days, oldest first
    for name in names:
        _touch(bdir, name)
    removed = backup._prune(bdir)
    keep = backup.BACKUP_KEEP
    assert [p.name for p in removed] == names[: len(names) - keep]
    assert sorted(p.name for p in bdir.glob("*.dump")) == names[len(names) - keep :]


def test_prune_keeps_newest_daily_plus_newest_pre_update_snapshot(
    bdir: Path,
) -> None:
    """Update-kind snapshots get their own slot: 7 dailies survive alongside the
    newest `.pre-update` snapshot instead of consuming a daily-dump slot."""
    dailies = [f"ava-202606{i:02d}-030000.dump" for i in range(1, 9)]  # 8 dailies
    snaps = [
        "ava-20260605T100000Z.pre-update.dump.gz.enc",
        "ava-20260607T100000Z.pre-update.dump.gz.enc",
    ]
    for name in dailies + snaps:
        _touch(bdir, name)
    removed = backup._prune(bdir)
    # the oldest daily (beyond the 7-window) and the older snapshot (only the
    # newest one is kept) are pruned; 7 dailies + the newest snapshot survive
    assert removed == [
        Path(bdir / "ava-20260601-030000.dump"),
        Path(bdir / "ava-20260605T100000Z.pre-update.dump.gz.enc"),
    ]
    assert (bdir / "ava-20260607T100000Z.pre-update.dump.gz.enc").exists()
    assert len(list(bdir.glob("*.dump"))) == backup.BACKUP_KEEP  # dailies only


def test_prune_with_snapshot_alone_keeps_newest_snapshot(bdir: Path) -> None:
    """No dailies yet (fresh cluster, first update) keeps the one snapshot."""
    snaps = [
        "ava-20260605T100000Z.pre-update.dump.gz.enc",
        "ava-20260607T100000Z.pre-update.dump.gz.enc",
    ]
    for name in snaps:
        _touch(bdir, name)
    removed = backup._prune(bdir)
    assert removed == [bdir / "ava-20260605T100000Z.pre-update.dump.gz.enc"]
    assert (bdir / "ava-20260607T100000Z.pre-update.dump.gz.enc").exists()


def test_prune_bounds_terminal_pitr_activation_snapshots(bdir: Path) -> None:
    activations = [
        _touch(
            bdir,
            f"ava-2026060{day}T100000Z.pitr-activation-"
            f"00000000-0000-0000-0000-00000000000{day}.dump.enc",
        )
        for day in range(1, backup.ACTIVATION_KEEP + 2)
    ]
    backup._prune(bdir)
    assert not activations[0].exists()
    assert all(path.exists() for path in activations[-backup.ACTIVATION_KEEP :])


def test_run_backup_pre_update_names_artifact(bdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-update snapshot carries the kind segment so prune can classify it."""
    monkeypatch.setattr(settings.general, "timezone", "Asia/Shanghai")

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **_kw: object) -> _Ok:
        if cmd[0].endswith("pg_dump"):
            Path(cmd[cmd.index("--file") + 1]).write_bytes(b"x")
        else:
            Path(cmd[cmd.index("-out") + 1]).write_bytes(b"encrypted dump")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)
    path = backup.run_backup(
        datetime(2026, 6, 10, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        db_url="dbname=ava",
        pre_update=True,
    )
    assert path.name == "ava-20260609T190000Z.pre-update.dump.enc"
    assert backup._is_pre_update(path)
    assert backup._is_pre_update(_touch(bdir, "ava-20260609T190000Z.dump.gz.enc")) is False


def test_run_backup_pitr_activation_has_independent_kind(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.general, "timezone", "Asia/Shanghai")

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **_kw: object) -> _Ok:
        output_flag = "--file" if cmd[0].endswith("pg_dump") else "-out"
        Path(cmd[cmd.index(output_flag) + 1]).write_bytes(b"backup")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)
    path = backup.run_backup(
        datetime(2026, 6, 10, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        db_url="dbname=ava",
        pitr_activation="11111111-1111-1111-1111-111111111111",
    )
    assert path.name == (
        "ava-20260609T190000Z.pitr-activation-11111111-1111-1111-1111-111111111111.dump.enc"
    )
    assert backup._is_activation(path)


def test_run_backup_failure_leaves_no_files(bdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Failed:
        returncode = 1
        stderr = "connection refused"

    monkeypatch.setattr(backup.subprocess, "run", lambda *_a, **_kw: _Failed())  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(RuntimeError, match="pg_dump exited 1"):
        backup.run_backup(_dt(2026, 6, 10, 3, 0), db_url="dbname=whatever")
    assert list(bdir.iterdir()) == []  # no .dump, no .partial


def test_run_backup_sweeps_stale_partials(bdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `.partial` left by an interrupted run (e.g. the process tree killed
    mid-rollout) is swept before the new dump is written, so it cannot pile up
    unnoticed: the name never matches `_NAME_RE`, so due/prune logic ignores it."""
    (bdir / "whatever-20260801-030000.dump.partial").write_bytes(b"stale")
    (bdir / "other-20260801-031500.dump.partial").write_bytes(b"stale")

    class _Failed:
        returncode = 1
        stderr = "connection refused"

    monkeypatch.setattr(backup.subprocess, "run", lambda *_a, **_kw: _Failed())  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(RuntimeError, match="pg_dump exited 1"):
        backup.run_backup(_dt(2026, 8, 2, 3, 0), db_url="dbname=whatever")
    assert not list(bdir.glob("*.partial"))  # both stale partials swept
    assert list(bdir.iterdir()) == []


@pytest.mark.skipif(not backup.pg_tool("pg_dump").exists(), reason="needs a native pg_dump binary")
def test_run_backup_real_dump_and_prune(bdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real pg_dump against the session's provisioned Postgres: dump lands under
    the managed name, the .partial intermediate is gone, and old dumps prune."""
    for i in range(1, backup.BACKUP_KEEP + 1):
        _touch(bdir, f"test-2026060{i}-030000.dump")

    _disable_offsite(monkeypatch)
    path = backup.run_backup(_dt(2026, 6, 10, 3, 0))

    assert path.parent == bdir
    assert backup._NAME_RE.match(path.name)
    assert path.stat().st_size > 0
    assert not list(bdir.glob("*.partial"))
    # BACKUP_KEEP pre-seeded + 1 new -> the oldest pre-seed pruned, KEEP remain.
    assert len(backup._managed_dumps(bdir)) == backup.BACKUP_KEEP
    assert not (bdir / "test-20260601-030000.dump").exists()
    # The fresh dump is restorable input: decrypt, then list its TOC directly
    # (current artifacts are raw custom dumps — no gzip layer).
    import subprocess

    restored = bdir / "listed.dump"
    cast(Any, backup).decrypt_artifact(path, restored)
    backup.gunzip_if_needed(restored)  # no-op on the new format; proves the contract
    assert restored.read_bytes().startswith(b"PGDMP"), "decrypted artifact is not a custom dump"
    listing = subprocess.run(  # noqa: S603
        [str(backup.pg_tool("pg_restore")), "--list", str(restored)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr


def test_run_backup_keeps_checkpoints_and_hides_db_password_from_argv(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recoverable dump includes checkpoints while its child argv omits the
    database password. A password exposed in `ps` is a credential leak; an
    excluded checkpoint table loses the sole copy of conversation history."""
    captured: dict[str, list[list[str]] | list[dict[str, str]] | int] = {
        "cmds": [],
        "envs": [],
    }

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _Ok:
        cast(list[list[str]], captured["cmds"]).append(cmd)
        cast(list[dict[str, str]], captured["envs"]).append(
            cast(dict[str, str], kwargs.get("env", {}))
        )
        captured["timeout"] = cast(int, kwargs.get("timeout"))
        if cmd[0].endswith("pg_dump"):
            # The real pg_dump writes the plaintext partial.
            Path(cmd[cmd.index("--file") + 1]).write_bytes(b"plaintext dump")
        else:
            # The encryption command would write its encrypted partial.
            Path(cmd[cmd.index("-out") + 1]).write_bytes(b"encrypted dump")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)
    password = "backup-password"  # noqa: S105 — test credential
    path = backup.run_backup(
        _dt(2026, 8, 8, 3, 0),
        db_url=f"postgresql://backup:{password}@db.example:5432/whatever",
    )

    # The final artifact must be encrypted and private rather than the plaintext
    # custom dump whose contents are readable by every account with filesystem access.
    assert path.name == "whatever-20260808T100000Z.dump.enc"
    assert path.stat().st_mode & 0o777 == 0o600
    assert bdir.stat().st_mode & 0o777 == 0o700

    cmds = cast(list[list[str]], captured["cmds"])
    pg_dump_cmd = next(cmd for cmd in cmds if cmd[0].endswith("pg_dump"))
    assert "--exclude-table" not in pg_dump_cmd
    dbname = pg_dump_cmd[pg_dump_cmd.index("--dbname") + 1]
    assert password not in " ".join(pg_dump_cmd)
    assert conninfo_to_dict(dbname) == {
        "dbname": "whatever",
        "host": "db.example",
        "port": "5432",
        "user": "backup",
    }
    envs = cast(list[dict[str, str]], captured["envs"])
    assert envs[cmds.index(pg_dump_cmd)]["PGPASSWORD"] == password

    # The archive compresses in-dump (zstd); there is no second gzip pass.
    assert all(cmd[0] != "gzip" for cmd in cmds)
    assert "--compress=zstd:3" in pg_dump_cmd
    assert "--format=custom" in pg_dump_cmd
    openssl_cmd = next(cmd for cmd in cmds if cmd[0].endswith("openssl"))
    assert {"enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-kfile"}.issubset(openssl_cmd)
    assert captured["timeout"] == backup._DUMP_TIMEOUT_S


def test_run_backup_keeps_database_password_out_of_argv(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pg_dump's connection target is visible in argv, so its password must be
    supplied only through the child-only PGPASSWORD environment variable."""
    captured: dict[str, object] = {}
    real_run = subprocess.run

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _Ok | subprocess.CompletedProcess[bytes]:
        if not cmd[0].endswith("pg_dump"):
            return real_run(cmd, **kwargs)  # type: ignore[arg-type, return-value]
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        Path(cmd[cmd.index("--file") + 1]).write_bytes(b"x")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)
    password = "password-not-in-argv"  # noqa: S105 — assertion sentinel, not a credential
    backup.run_backup(
        _dt(2026, 8, 8, 3, 0),
        db_url=f"postgresql://ava:{password}@127.0.0.1:5433/ava",
    )

    cmd = cast(list[str], captured["cmd"])
    env = cast(dict[str, str], captured["env"])
    assert password not in " ".join(cmd)
    assert env["PGPASSWORD"] == password


def test_encrypted_artifact_decrypts_to_original_dump(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published artifact reverses exactly to pg_dump's custom-format bytes.

    This fails if the key-file derivation, cipher invocation, or artifact name
    changes incompatibly with the restore procedure. The artifact carries no
    gzip layer — `gunzip_if_needed` must leave it untouched.
    """
    original_dump = b"custom-format-pg-dump\x00contents"
    real_run = subprocess.run

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_pg_dump(cmd: list[str], **kwargs: object) -> _Ok | subprocess.CompletedProcess[bytes]:
        if cmd[0].endswith("pg_dump"):
            Path(cmd[cmd.index("--file") + 1]).write_bytes(original_dump)
            return _Ok()
        return real_run(cmd, **kwargs)  # type: ignore[arg-type, return-value]

    monkeypatch.setattr(backup.subprocess, "run", _fake_pg_dump)
    _disable_offsite(monkeypatch)
    artifact = backup.run_backup(_dt(2026, 8, 8, 3, 0), db_url="dbname=whatever")

    assert artifact.name.endswith(".dump.enc")
    key_file = bdir / "decrypt.key"
    key_file.write_text(
        hashlib.sha256(settings.data_plane.cluster_secret.encode()).hexdigest(), encoding="utf-8"
    )
    key_file.chmod(0o600)
    custom_dump = bdir / "restored.dump"
    decrypted = real_run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-salt",
            "-kfile",
            str(key_file),
            "-in",
            str(artifact),
            "-out",
            str(custom_dump),
        ],
        capture_output=True,
        check=False,
    )
    assert decrypted.returncode == 0, decrypted.stderr.decode()
    backup.gunzip_if_needed(custom_dump)
    assert custom_dump.read_bytes() == original_dump


def test_gunzip_if_needed_decompresses_legacy_artifact_layer(
    bdir: Path,
) -> None:
    """A legacy `.dump.gz.enc` artifact decrypts to gzip bytes; the helper
    strips that layer in place so the restore procedure stays uniform."""
    import gzip

    raw = b"custom-format-pg-dump\x00contents"
    layered = bdir / "legacy.dump"
    layered.write_bytes(gzip.compress(raw))
    backup.gunzip_if_needed(layered)
    assert layered.read_bytes() == raw
    assert layered.stat().st_mode & 0o777 == 0o600  # never widened by umask
    # A current-format dump (no gzip magic) passes through untouched.
    plain = bdir / "current.dump"
    plain.write_bytes(raw)
    backup.gunzip_if_needed(plain)
    assert plain.read_bytes() == raw


def test_offsite_publish_goes_through_the_store_contract(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The off-site leg publishes through the shared BlobStore contract: an
    if-absent object under the logical root, store-verified ACK, and the store
    source reads exactly the encrypted artifact bytes."""
    artifact = _touch(bdir, "ava-20260608T100000Z.dump.enc")
    artifact.write_bytes(b"encrypted artifact")
    calls: list[dict[str, object]] = []

    class _Store:
        def put_base_if_absent(
            self,
            *,
            source: object,
            object_name: str,
            metadata: Mapping[str, str],
            cancelled: object = None,
        ) -> RemoteObjectAck:
            calls.append({"source": source, "object_name": object_name, "metadata": dict(metadata)})
            return RemoteObjectAck(
                object_name=object_name,
                pin_token="gen-7",  # noqa: S106 — fake store identity, not a secret
                size=artifact.stat().st_size,
                checksum=ObjectChecksum(MD5, "0" * 32),
                metadata=dict(metadata),
                created=True,
            )

    class _Group:
        def restartable_streaming_object_store(self) -> _Store:
            return _Store()

    monkeypatch.setattr(store_factory, "get_store_group", _Group)

    published = backup._publish_offsite(artifact)

    assert published == f"{backup._REMOTE_ROOT}/{artifact.name}"
    assert calls[0]["object_name"] == published
    assert calls[0]["metadata"] == {"ava-artifact-kind": "logical-backup"}
    source = cast(Any, calls[0]["source"])
    assert source.ciphertext_size == len(b"encrypted artifact")
    assert b"".join(source.iter_chunks()) == b"encrypted artifact"
    # The local artifact is still the primary copy after a successful publish.
    assert artifact.read_bytes() == b"encrypted artifact"


def test_offsite_store_unavailable_keeps_local_artifact(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The off-site leg is optional: an unconstructable store cannot turn a
    successful local backup into a failed backup or remove its only local
    artifact."""
    artifact = _touch(bdir, "ava-20260608T100000Z.dump.enc")
    artifact.write_bytes(b"encrypted artifact")

    def _no_store_group() -> Any:
        raise RuntimeError("no backup store configured")

    monkeypatch.setattr(store_factory, "get_store_group", _no_store_group)

    assert backup._publish_offsite(artifact) is None
    assert artifact.read_bytes() == b"encrypted artifact"


def test_run_backup_publishes_offsite_via_store_contract(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline publishes the encrypted artifact through the store after
    encryption; the published name mirrors the local managed name."""
    published: list[str] = []

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _Ok:
        if cmd[0].endswith("pg_dump"):
            Path(cmd[cmd.index("--file") + 1]).write_bytes(b"plaintext dump")
        else:
            Path(cmd[cmd.index("-out") + 1]).write_bytes(b"encrypted dump")
        return _Ok()

    class _Store:
        def put_base_if_absent(
            self,
            *,
            source: object,
            object_name: str,
            metadata: Mapping[str, str],
            cancelled: object = None,
        ) -> RemoteObjectAck:
            published.append(object_name)
            return RemoteObjectAck(
                object_name=object_name,
                pin_token="p",  # noqa: S106 — fake store identity, not a secret
                size=13,
                checksum=ObjectChecksum(MD5, "0" * 32),
                metadata=dict(metadata),
                created=True,
            )

    class _Group:
        def restartable_streaming_object_store(self) -> _Store:
            return _Store()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)
    monkeypatch.setattr(store_factory, "get_store_group", _Group)

    artifact = backup.run_backup(_dt(2026, 8, 8, 3, 0), db_url="dbname=whatever")

    assert published == [f"{backup._REMOTE_ROOT}/{artifact.name}"]
    assert artifact.read_bytes() == b"encrypted dump"


def test_run_backup_forwards_requested_timeout(bdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded caller's deadline applies to every backup pipeline process."""
    timeouts: list[float] = []

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _Ok:
        timeouts.append(cast(float, kwargs["timeout"]))
        if cmd[0].endswith("pg_dump"):
            Path(cmd[cmd.index("--file") + 1]).write_bytes(b"plaintext dump")
        else:
            Path(cmd[cmd.index("-out") + 1]).write_bytes(b"encrypted dump")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)

    backup.run_backup(_dt(2026, 8, 8, 3, 0), db_url="dbname=whatever", timeout_s=123.0)

    assert timeouts == [123.0, 123.0]  # pg_dump + encryption — the gzip stage is gone


def test_backup_lock_reentrant_same_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot may take the lock before `run_backup` takes it again."""
    ava_home = tmp_path / "ava-home"
    monkeypatch.setattr(settings.general, "ava_home", ava_home)

    with backup.backup_lock(), backup.backup_lock(timeout_s=0.5):
        pass

    with backup.backup_lock(timeout_s=0.5):
        pass


def test_backup_lock_cross_process_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduler dump waits for a rollout snapshot already holding the lock."""
    ava_home = tmp_path / "ava-home"
    monkeypatch.setattr(settings.general, "ava_home", ava_home)
    ready = tmp_path / "ready"
    holder = _spawn_backup_lock_holder(ava_home, ready, hold_s=2.0)
    try:
        _await_backup_lock_holder(ready, holder)

        started = time.monotonic()
        with backup.backup_lock(timeout_s=30):
            waited = time.monotonic() - started
        assert waited >= 0.4, (
            f"took the backup lock while another process held it (waited {waited:.2f}s)"
        )
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)


def test_backup_lock_timeout_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged snapshot produces a bounded failure rather than an unbounded wait."""
    ava_home = tmp_path / "ava-home"
    monkeypatch.setattr(settings.general, "ava_home", ava_home)
    ready = tmp_path / "ready"
    holder = _spawn_backup_lock_holder(ava_home, ready, hold_s=30.0)
    try:
        _await_backup_lock_holder(ready, holder)

        started = time.monotonic()
        with pytest.raises(LockTimeoutError), backup.backup_lock(timeout_s=0.5):
            pytest.fail("took a backup lock another process was holding")
        waited = time.monotonic() - started
        assert 0.3 <= waited < 5
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)


def test_run_backup_serializes_dump_creation(bdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The public entry point holds the cross-process lock around its body."""
    events: list[str] = []
    artifact = bdir / "verified.dump.enc"

    @contextmanager
    def _backup_lock(**_kwargs: object):
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    def _run_backup(_now: datetime | None = None, **_kwargs: object) -> Path:
        events.append("backup-body")
        return artifact

    monkeypatch.setattr(backup, "backup_lock", _backup_lock)
    monkeypatch.setattr(backup, "_run_backup", _run_backup)

    assert backup.run_backup(_dt(2026, 8, 8, 3, 0), db_url="dbname=whatever") == artifact
    assert events == ["lock-enter", "backup-body", "lock-exit"]


def test_run_backup_avoids_overwriting_a_same_second_dump(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second managed writer publishes a new encrypted name, not a replacement."""
    existing = _touch(bdir, "whatever-20260808T100000Z.dump.enc")

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _Ok:
        if cmd[0].endswith("pg_dump"):
            Path(cmd[cmd.index("--file") + 1]).write_bytes(b"plaintext dump")
        else:
            Path(cmd[cmd.index("-out") + 1]).write_bytes(b"encrypted dump")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)

    created = backup.run_backup(_dt(2026, 8, 8, 3, 0), db_url="dbname=whatever")

    assert created.name == "whatever-20260808T100001Z.dump.enc"
    assert existing.read_bytes() == b"x"
    assert created.read_bytes() == b"encrypted dump"
