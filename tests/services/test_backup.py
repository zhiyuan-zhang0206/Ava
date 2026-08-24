"""services/backup.py — due-ness, prune, foreign-file safety, failure cleanup,
and a real pg_dump round-trip against the session's provisioned Postgres.

Every clock here is pinned: the module decides *when* in cluster time
(`AVA_TIMEZONE`) and *names* dumps in UTC, so a test that let either fall back
to the host's timezone would pass or fail by which machine ran it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from services import backup
from shared.config import settings

_CLUSTER_TZ = "America/Los_Angeles"


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
        Path(cmd[cmd.index("--file") + 1]).write_bytes(b"x")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)
    path = backup.run_backup(
        datetime(2026, 6, 10, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai")), db_url="dbname=ava"
    )
    assert path.name == "ava-20260609T190000Z.dump"


def test_run_backup_repairs_storage_permissions(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup directory and completed dump are owner-only despite umask drift."""
    bdir.chmod(0o755)

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **_kw: object) -> _Ok:
        partial = Path(cmd[cmd.index("--file") + 1])
        partial.write_bytes(b"dump")
        partial.chmod(0o644)
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


def test_run_backup_failure_leaves_no_files(bdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Failed:
        returncode = 1
        stderr = "connection refused"

    monkeypatch.setattr(backup.subprocess, "run", lambda *_a, **_kw: _Failed())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
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

    monkeypatch.setattr(backup.subprocess, "run", lambda *_a, **_kw: _Failed())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(RuntimeError, match="pg_dump exited 1"):
        backup.run_backup(_dt(2026, 8, 2, 3, 0), db_url="dbname=whatever")
    assert not list(bdir.glob("*.partial"))  # both stale partials swept
    assert list(bdir.iterdir()) == []


@pytest.mark.skipif(not backup.pg_tool("pg_dump").exists(), reason="needs a native pg_dump binary")
def test_run_backup_real_dump_and_prune(bdir: Path) -> None:
    """Real pg_dump against the session's provisioned Postgres: dump lands under
    the managed name, the .partial intermediate is gone, and old dumps prune."""
    for i in range(1, backup.BACKUP_KEEP + 1):
        _touch(bdir, f"test-2026060{i}-030000.dump")

    path = backup.run_backup(_dt(2026, 6, 10, 3, 0))

    assert path.parent == bdir
    assert backup._NAME_RE.match(path.name)
    assert path.stat().st_size > 0
    assert not list(bdir.glob("*.partial"))
    # BACKUP_KEEP pre-seeded + 1 new -> the oldest pre-seed pruned, KEEP remain.
    assert len(backup._managed_dumps(bdir)) == backup.BACKUP_KEEP
    assert not (bdir / "test-20260601-030000.dump").exists()
    # The fresh dump is restorable input: pg_restore can list its TOC.
    import subprocess

    listing = subprocess.run(  # noqa: S603
        [str(backup.pg_tool("pg_restore")), "--list", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr


def test_run_backup_excludes_checkpoint_tables_and_uses_headroom_timeout(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daily dump excludes the LangGraph runtime tables (checkpoint_blobs
    alone outgrew the old 30-min ceiling and dead-looped the backup) and runs
    under the 60-min headroom timeout. #1035 incident pin."""
    captured: dict[str, list[str] | int] = {}

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _Ok:
        captured["cmd"] = cmd
        captured["timeout"] = cast(int, kwargs.get("timeout"))
        # the real pg_dump would write the partial; fake it so the rename lands
        file_idx = cmd.index("--file") + 1
        Path(cmd[file_idx]).write_bytes(b"x")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)
    backup.run_backup(_dt(2026, 8, 8, 3, 0), db_url="dbname=whatever")

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    exclusions = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--exclude-table"]
    assert exclusions == list(backup._EXCLUDE_TABLES)
    assert captured["timeout"] == backup._DUMP_TIMEOUT_S
