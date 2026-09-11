from pathlib import Path

import pytest

import shared.private_storage
from shared.config import settings
from shared.config.general import GeneralSettings
from shared.envfile import (
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


def test_snapshot_filename_stamps_cluster_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The backup filename's wall clock follows the cluster timezone (user
    ruling 2026-08-27), so an operator reading backup names sees the same
    clock as every other surface."""

    import datetime as _dt
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(
        settings, "general", GeneralSettings.model_construct(timezone="Asia/Shanghai")
    )
    env = tmp_path / ".env"
    env.write_text("SECRET=abc\n")
    # Snapshot before/after the call so the midnight boundary cannot race the
    # assertion: the stamp must be the cluster-zone wall date at call time.
    before = _dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    dest = snapshot_env(env)
    after = _dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    assert dest is not None
    stamp = dest.name[len(".env.") :].split("-")[0]  # YYYYMMDD
    assert len(stamp) == 8
    assert stamp in {before.strftime("%Y%m%d"), after.strftime("%Y%m%d")}


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
    real_replace = shared.private_storage.os.replace
    replaced: list[Path] = []

    def _replace(source: str | Path, destination: str | Path) -> None:
        assert Path(destination) == env
        assert env.read_text() == "SECRET=old\nKEEP=1\n"
        assert Path(source).read_text() == "SECRET=new\nKEEP=1\n"
        real_replace(source, destination)
        replaced.append(Path(destination))

    monkeypatch.setattr(shared.private_storage.os, "replace", _replace)

    upsert_env(env, {"SECRET": "new"})

    assert replaced == [env]


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
