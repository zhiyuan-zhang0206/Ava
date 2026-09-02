"""Warning-only $AVA_HOME ownership preflight contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands import _converge
from cli.commands import _ownership_preflight as _ownership
from cli.commands._converge_spec import ConvergeCtx


def _ctx(tmp_path: Path) -> ConvergeCtx:
    home = tmp_path / "home"
    home.mkdir()
    for name in (".env", "logs", "configs", "secrets", "source"):
        path = home / name
        if name == ".env":
            path.touch()
        else:
            path.mkdir()
    return ConvergeCtx(repo=tmp_path / "repo", ava_home=home, roles=frozenset({"gateway"}))


def test_collect_ownership_warnings_names_only_non_user_owned_key_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _ctx(tmp_path)
    foreign = ctx.ava_home / "source"

    def owner_uid(path: Path) -> int:
        return 0 if path == foreign else 501

    def repair_command(path: Path) -> str:
        return f"sudo chown -R ava:staff {path}"

    monkeypatch.setattr(_ownership.os, "getuid", lambda: 501)
    monkeypatch.setattr(_ownership, "_owner_uid", owner_uid)
    monkeypatch.setattr(_ownership, "_repair_command", repair_command)

    warnings = _ownership.collect_ownership_warnings(ctx)

    assert warnings == [
        f"{foreign}: owned by uid 0, current uid 501; repair with: "
        f"sudo chown -R ava:staff {foreign}"
    ]


def test_collect_ownership_warnings_skips_a_missing_source_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _ctx(tmp_path)
    (ctx.ava_home / "source").rmdir()

    def owner_uid(_path: Path) -> int:
        return 501

    monkeypatch.setattr(_ownership.os, "getuid", lambda: 501)
    monkeypatch.setattr(_ownership, "_owner_uid", owner_uid)

    assert _ownership.collect_ownership_warnings(ctx) == []


def test_collect_ownership_warnings_skips_non_posix_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _ctx(tmp_path)

    class Backend:
        def is_posix(self) -> bool:
            return False

    monkeypatch.setattr(_ownership, "get_backend", Backend)

    assert _ownership.collect_ownership_warnings(ctx) == []


def test_ownership_preflight_prints_and_logs_without_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _ctx(tmp_path)

    def warnings(_ctx: ConvergeCtx) -> list[str]:
        return ["/home/ava/source: owned by uid 0, current uid 501; repair with: sudo chown"]

    monkeypatch.setattr(_ownership, "collect_ownership_warnings", warnings)

    _ownership.ensure_ownership_preflight(ctx)

    err = capsys.readouterr().err
    assert "OWNERSHIP PREFLIGHT" in err
    assert "start continues" in err
    log = ctx.ava_home / "logs" / "ownership_preflight.log"
    assert log.exists()
    assert log.read_text().endswith("repair with: sudo chown\n")


def test_ownership_preflight_never_fails_converge_when_its_scan_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _ctx(tmp_path)

    def explode(_ctx: ConvergeCtx) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(_ownership, "collect_ownership_warnings", explode)

    _ownership.ensure_ownership_preflight(ctx)

    assert "ownership preflight skipped: boom" in capsys.readouterr().err


def test_ownership_preflight_is_a_file_only_converge_step() -> None:
    step = _converge.CONVERGE_STEPS[0]

    assert step.name == "$AVA_HOME ownership preflight"
    assert step.apply is _ownership.ensure_ownership_preflight
    assert step.requires_unit_config is False
