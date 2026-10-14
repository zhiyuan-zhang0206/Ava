"""Warning-only $AVA_HOME ownership preflight contracts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.host.system.tests.fake_commands import fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge import ownership_preflight as _ownership
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.lifecycle import converge_host
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _ctx(
    tmp_path: Path,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> ConvergeCtx:
    home = tmp_path / "home"
    home.mkdir()
    for name in (".env", "logs", "configs", "secrets", "source"):
        path = home / name
        if name == ".env":
            path.touch()
        else:
            path.mkdir()
    return ConvergeCtx(
        repo=tmp_path / "repo",
        ava_home=home,
        roles=frozenset({"gateway"}),
        config=command_runtime.config,
        database_factory=operator_database,
        producer=producer,
        machine=command_runtime.machine,
        helper=refuse_helper,
        host=fake_host(),
        services=frozenset(),
        timers=ManualTimers(),
    )


def test_collect_ownership_warnings_names_only_non_user_owned_key_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    foreign = ctx.ava_home / "source"

    def path_owner_uid(path: Path) -> int:
        return 0 if path == foreign else 501

    def repair_command(path: Path) -> str:
        return f"sudo chown -R ava:staff {path}"

    monkeypatch.setattr(_ownership.os, "getuid", lambda: 501)
    monkeypatch.setattr(_ownership, "path_owner_uid", path_owner_uid)
    monkeypatch.setattr(_ownership, "repair_command", repair_command)

    warnings = _ownership.collect_ownership_warnings(ctx)

    assert warnings == [
        f"{foreign}: owned by uid 0, current uid 501; repair with: "
        f"sudo chown -R ava:staff {foreign}"
    ]


def test_collect_ownership_warnings_skips_a_missing_source_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    (ctx.ava_home / "source").rmdir()

    def path_owner_uid(_path: Path) -> int:
        return 501

    monkeypatch.setattr(_ownership.os, "getuid", lambda: 501)
    monkeypatch.setattr(_ownership, "path_owner_uid", path_owner_uid)

    assert _ownership.collect_ownership_warnings(ctx) == []


def test_ownership_preflight_prints_and_logs_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    def explode(_ctx: ConvergeCtx) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(_ownership, "collect_ownership_warnings", explode)

    _ownership.ensure_ownership_preflight(ctx)

    assert "ownership preflight skipped: boom" in capsys.readouterr().err


def test_ownership_preflight_is_a_file_only_converge_step() -> None:
    step = converge_host.CONVERGE_STEPS[0]

    assert step.name == "$AVA_HOME ownership preflight"
    assert step.apply is _ownership.ensure_ownership_preflight
    assert step.requires_unit_config is False
