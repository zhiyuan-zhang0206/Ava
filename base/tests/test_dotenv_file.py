import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import base.host.private_storage
from base.config import settings
from base.config.domains.general import GeneralSettings
from base.host.env.dotenv_file import (
    ENV_BACKUP_KEEP,
    env_line_export_prefix,
    env_line_key,
    remove_env,
    snapshot_env,
    upsert_env,
)


def _backups(env_path: Path) -> list[Path]:
    return sorted((env_path.parent / "backups" / "env").glob(".env.*"))


def test_upsert_adds_and_updates(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("AVA_DB_URL=old\nKEEP=1\n")
    upsert_env(f, {"AVA_DB_URL": "new", "AVA_CLUSTER": "t1"})
    text = f.read_text()
    assert "AVA_DB_URL=new" in text
    assert "AVA_CLUSTER=t1" in text
    assert "KEEP=1" in text
    assert text.count("AVA_DB_URL=") == 1


def test_upsert_creates_file_and_preserves_comments(tmp_path: Path):
    f = tmp_path / "sub" / ".env"
    upsert_env(f, {"A": "1"})
    assert f.read_text() == "A=1\n"

    f.write_text("# comment\n\nA=1\n")
    upsert_env(f, {"B": "2"})
    text = f.read_text()
    assert "# comment" in text
    assert "" in text.splitlines()  # blank line preserved
    assert "A=1" in text and "B=2" in text


# ─── owner-only writes (audit round-2 security P1-3) ───


def test_upsert_writes_0600(tmp_path: Path):
    """A .env write must be owner-only regardless of umask — .env is the
    cluster's only on-disk secret copy (snapshot_env already enforced this;
    the main file now does too)."""
    f = tmp_path / ".env"
    f.write_text("OLD=1\n")
    upsert_env(f, {"NEW": "2"})
    assert oct(f.stat().st_mode)[-3:] == "600"


def test_upsert_creates_new_file_0600(tmp_path: Path):
    f = tmp_path / "sub" / ".env"
    upsert_env(f, {"A": "1"})
    assert oct(f.stat().st_mode)[-3:] == "600"


def test_remove_env_keeps_0600(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("KEEP=1\nDROP=2\n")
    f.chmod(0o600)
    remove_env(f, {"DROP"})
    assert oct(f.stat().st_mode)[-3:] == "600"
    assert f.read_text() == "KEEP=1\n"


def test_remove_env_repairs_0644(tmp_path: Path):
    """A pre-existing 0644 .env is tightened by the next write."""
    f = tmp_path / ".env"
    f.write_text("A=1\nB=2\n")
    f.chmod(0o644)
    remove_env(f, {"B"})
    assert oct(f.stat().st_mode)[-3:] == "600"


# ─── snapshot_env (the .env backup safety net) ───


def test_snapshot_absent_or_blank_is_noop(tmp_path: Path):
    env = tmp_path / ".env"
    assert snapshot_env(env) is None  # absent
    env.write_text("   \n")
    assert snapshot_env(env) is None  # blank
    assert not (tmp_path / "backups").exists()


def test_snapshot_backs_up_content_0600(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("SECRET=abc\n")
    dest = snapshot_env(env)
    assert dest is not None
    assert dest.read_text() == "SECRET=abc\n"
    assert oct(dest.stat().st_mode)[-3:] == "600"


# A backup instant's calendar date in these two zones never agrees: the gap
# between UTC+14 and UTC-12 is 26 hours, more than a full day, so one zone has
# always already rolled past its own midnight relative to the other — proven
# for every real instant, not just the moment a test happens to run (verified
# by brute force across 200k random instants spanning 20 years).
_HOST_ZONE = "Pacific/Kiritimati"  # UTC+14, no DST
_DECOY_CLUSTER_ZONE = "Etc/GMT+12"  # UTC-12, no DST


def test_snapshot_filename_stamps_the_host_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup filename's wall clock is the HOST's, never the cluster's —
    the 2026-09-28 ruling exception to the 2026-08-27 one-cluster-clock rule
    (decisions/2026-09-28-env-backup-names-use-the-host-clock.md): `.env` is
    written before a process's identity, and its authoritative cluster clock,
    are established (a unit's first `ava start`), and reading the cluster clock there
    means loading runtime config — a `GET /api/bootstrap` on a pure runner —
    which `snapshot_env` cannot afford.

    Pins the process to a host zone 26 hours from a decoy
    `settings.general.timezone` (see `_HOST_ZONE` / `_DECOY_CLUSTER_ZONE`
    above: far enough apart that their calendar dates never agree, at any
    instant) and asserts the stamp follows the HOST date, never the decoy's —
    deterministic under both `TZ=UTC` and the runner's own default zone, since
    the host zone is pinned explicitly rather than inherited from the
    environment.
    """
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only")

    original_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", _HOST_ZONE)
        time.tzset()
        monkeypatch.setattr(
            settings,
            "general",
            GeneralSettings.model_construct(timezone=_DECOY_CLUSTER_ZONE),
        )
        env = tmp_path / ".env"
        env.write_text("SECRET=abc\n")
        # Snapshot before/after the call so the pinned host zone's own
        # midnight boundary cannot race the assertion.
        before = datetime.now().astimezone()
        dest = snapshot_env(env)
        after = datetime.now().astimezone()
        decoy_now = datetime.now(ZoneInfo(_DECOY_CLUSTER_ZONE))

        assert dest is not None
        stamp = dest.name[len(".env.") :].split("-")[0]  # YYYYMMDD
        assert len(stamp) == 8
        assert stamp in {before.strftime("%Y%m%d"), after.strftime("%Y%m%d")}
        assert stamp != decoy_now.strftime("%Y%m%d")
    finally:
        # monkeypatch's own TZ-env undo does not re-run tzset(), so the C
        # library's cached zone would otherwise leak into later tests (the
        # same gotcha base/config/tests/test_cluster_tz.py's _restore_process_tz
        # documents).
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


def test_snapshot_dedupes_identical(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    snapshot_env(env)
    assert snapshot_env(env) is None  # unchanged since last snapshot -> skipped
    assert len(_backups(env)) == 1


def test_snapshot_records_each_distinct_state(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    snapshot_env(env)
    env.write_text("A=2\n")
    snapshot_env(env)
    backups = _backups(env)
    assert len(backups) == 2
    assert {b.read_text() for b in backups} == {"A=1\n", "A=2\n"}


def test_snapshot_prunes_to_keep(tmp_path: Path):
    env = tmp_path / ".env"
    backup_dir = tmp_path / "backups" / "env"
    backup_dir.mkdir(parents=True)
    # Pre-seed keep+3 old snapshots; names sort before any real timestamp ('0'<'2').
    for i in range(ENV_BACKUP_KEEP + 3):
        (backup_dir / f".env.{i:04d}").write_text(f"old-{i}\n")
    env.write_text("NEW=1\n")
    snapshot_env(env)
    remaining = _backups(env)
    assert len(remaining) == ENV_BACKUP_KEEP
    assert remaining[-1].read_text() == "NEW=1\n"  # newest survived the prune


def test_upsert_env_snapshots_before_write(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("SECRET=orig\n")
    upsert_env(env, {"SECRET": "new"})
    assert env.read_text().strip() == "SECRET=new"
    backups = _backups(env)
    assert len(backups) == 1
    assert backups[0].read_text() == "SECRET=orig\n"  # pre-write state recoverable


def test_upsert_atomically_replaces_the_complete_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = tmp_path / ".env"
    env.write_text("SECRET=old\nKEEP=1\n")
    real_replace = base.host.private_storage.os.replace
    replaced: list[Path] = []

    def _replace(source: str | Path, destination: str | Path) -> None:
        assert Path(destination) == env
        assert env.read_text() == "SECRET=old\nKEEP=1\n"
        assert Path(source).read_text() == "SECRET=new\nKEEP=1\n"
        real_replace(source, destination)
        replaced.append(Path(destination))

    monkeypatch.setattr(base.host.private_storage.os, "replace", _replace)

    upsert_env(env, {"SECRET": "new"})

    assert replaced == [env]


# ─── skip-when-unchanged: a no-op upsert is not a write (task #3637) ───


def test_upsert_noop_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A byte-identical upsert must not rewrite, snapshot, or record.

    The repeated converge (every `ava start`, every boot-retry attempt) presents
    the same value again and again; before the skip it rewrote the file and
    appended an `old == new` audit record each time — the WSL noise of #3637.
    """
    from base.host.env import dotenv_file

    f = tmp_path / ".env"
    f.write_text("A=1\nKEEP=2\n")
    stamp = f.stat().st_mtime_ns
    writes: list[bytes] = []

    def _record_write(_path: Path, data: bytes) -> None:
        writes.append(data)

    monkeypatch.setattr(dotenv_file, "write_private_bytes", _record_write)

    dotenv_file.upsert_env(f, {"A": "1"}, audit_site="test_site")

    assert writes == []
    assert f.read_text() == "A=1\nKEEP=2\n"
    assert f.stat().st_mtime_ns == stamp
    assert _backups(f) == []
    assert not (tmp_path / ".env.audit.jsonl").exists()


def test_upsert_noop_still_normalizes_a_quirky_line(tmp_path: Path) -> None:
    """Skip is byte-level: a quoted or oddly-spaced rendering is still a change.

    The decoded value already matches, but the bytes do not — that write happens
    (normalizing the line), so the skip can never strand a non-canonical line.
    """
    f = tmp_path / ".env"
    f.write_text('A="1"\nB = 2\n')
    upsert_env(f, {"A": "1", "B": "2"})
    assert f.read_text() == "A=1\nB=2\n"


def test_upsert_noop_is_decided_on_the_whole_result(tmp_path: Path) -> None:
    """One changed key among unchanged ones still writes the file."""
    f = tmp_path / ".env"
    f.write_text("A=1\nB=2\n")
    upsert_env(f, {"A": "1", "B": "3"})
    assert f.read_text() == "A=1\nB=3\n"


def test_upsert_noop_leaves_a_symlinked_env_untouched(tmp_path: Path) -> None:
    """A no-op must not mutate the node: the old unconditional write replaced a
    symlinked `.env` with a regular file (`os.replace` swaps the link itself);
    the skip leaves the link and its target exactly as they were."""
    target = tmp_path / "real.env"
    target.write_text("A=1\n")
    link = tmp_path / ".env"
    link.symlink_to(target)

    upsert_env(link, {"A": "1"})

    assert link.is_symlink()
    assert link.read_text() == "A=1\n"
    assert target.read_text() == "A=1\n"


# ─── env_line_key: the settings parser's key grammar (export prefix, #2981) ───


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("KEY=1", "KEY"),
        ("KEY = 1", "KEY"),
        ("  KEY=1", "KEY"),
        ("export KEY=1", "KEY"),
        ("export  KEY=1", "KEY"),
        ("export\tKEY=1", "KEY"),
        ("\texport KEY = 1", "KEY"),
        ("export 'KEY'=7", "KEY"),
        ("'KEY'=1", "KEY"),
        ('"KEY"=1', '"KEY"'),
        ("exportKEY=1", "exportKEY"),
        ("export=1", "export"),
        ("exportED_KEY=1", "exportED_KEY"),
        ("KEY=1 # note", "KEY"),
        ('KEY="18133', "KEY"),
        ("# KEY=1", None),
        ("", None),
        ("KEY", None),
        ("KEY # note", None),
        ("EXPORT KEY=1", None),
        ("export KEY # note", None),
        ("''=1", None),
        ("'KEY=1", None),
    ],
)
def test_env_line_key_follows_the_parser_grammar(line: str, expected: str | None) -> None:
    """`export` plus whitespace is a prefix, never part of the key — and never
    the bare `removeprefix("export")` trap (`exported_*` keeps its name). A
    double-quoted key keeps its quotes, exactly as the parser leaves it, and an
    undecodable value still reports its key so a reader can say so (#2981)."""
    assert env_line_key(line) == expected


def test_env_line_key_agrees_with_dotenv_on_assignments() -> None:
    """The extractor and python-dotenv must not drift: for every line the parser
    itself parses into an assignment, the key we report is the parser's key."""
    from io import StringIO

    from dotenv import dotenv_values

    for line in (
        "KEY=1",
        "KEY = 1",
        "export KEY=1",
        'export  KEY = "2"',
        "export\tKEY=6",
        "export 'KEY'=5",
        "'KEY'=3",
        '"KEY"=4',
        "exportKEY=7",
        "export=8",
        "KEY= # c",
    ):
        parsed = list(dotenv_values(stream=StringIO(line), interpolate=False))
        assert env_line_key(line) == parsed[0], line


def test_upsert_rewrites_an_export_prefixed_line_in_place(tmp_path: Path) -> None:
    """An export-prefixed assignment is the same key: it is replaced in place —
    no duplicate appended — and the operator's prefix survives (a shell consumer
    of the file would otherwise be silently switched off)."""
    f = tmp_path / ".env"
    f.write_text("export AVA_DB_URL=old\nKEEP=1\nexport\tAVA_CLUSTER=old2\n")
    upsert_env(f, {"AVA_DB_URL": "new", "AVA_CLUSTER": "t1"})
    assert f.read_text() == "export AVA_DB_URL=new\nKEEP=1\nexport AVA_CLUSTER=t1\n"


def test_upsert_leaves_lookalike_export_keys_alone(tmp_path: Path) -> None:
    """`exportED_KEY` is a key of its own — stripping the prefix must not eat it."""
    f = tmp_path / ".env"
    f.write_text("exportED_KEY=keep\n")
    upsert_env(f, {"AVA_DB_URL": "new"})
    assert f.read_text() == "exportED_KEY=keep\nAVA_DB_URL=new\n"


def test_remove_env_drops_export_prefixed_lines(tmp_path: Path) -> None:
    """A retired key actually leaves the surface even when it was hand-written
    with the export prefix — Settings reads both forms as the same key."""
    f = tmp_path / ".env"
    f.write_text("export AVA_PGBOUNCER_PORT=6543\nKEEP=1\n")
    remove_env(f, {"AVA_PGBOUNCER_PORT"})
    assert f.read_text() == "KEEP=1\n"


def test_env_line_export_prefix_flags_only_real_export_prefixed_lines() -> None:
    assert env_line_export_prefix("export KEY=1") == "export "
    assert env_line_export_prefix("export\tKEY=1") == "export "
    assert env_line_export_prefix("  export  KEY=1") == "export "
    assert env_line_export_prefix("exportKEY=1") == ""
    assert env_line_export_prefix("export=1") == ""
    assert env_line_export_prefix("KEY=1") == ""
