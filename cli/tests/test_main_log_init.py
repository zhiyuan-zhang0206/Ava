"""The verbs that bring a unit up keep their loguru records.

Importing `base.log` drops loguru's default handler, so a CLI process that
opens no sink discards every record it writes through loguru. On the start
path some warnings exist only there: a skipped pgvector pre-create, untracked
migration files that will not be applied. `ava start`, `ava restart`
and `ava lgtm on|off` (every in-process `cmd_start`)
open the sinks `base.log.init_cli_process` gives: stderr, the unit's
`logs/cli-<verb>.log` and the event pipeline, without a `service_started`
row. Other verbs print to the caller's terminal and open none.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
import yaml

import cli.main as _main
from base import telemetry
from base.agents.context.clients import DatabaseFactory
from base.cluster import postgres
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.telemetry import EventPipeline
from cli import parsers
from cli.database import operator_database_factory, operator_event_pipeline
from cli.parsers import build_parser

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The real `ava init` then `ava start` dispatch into a fresh home: identity
# publication, then the sinks, then a service phase replaced by the real
# pgvector pre-create against a port nothing listens on.
_FIRST_START = r"""
import os
import sys
import types
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from cli import main, start_intent
from base import cluster
home = Path(os.environ["AVA_HOME"])
checkout = home.parent / "checkout"
checkout.mkdir()
start_intent._checkout = lambda: checkout
cluster.port_free = lambda _port: True
def start(**kwargs):
    from base.cluster import ensure_pgvector_extension
    ensure_pgvector_extension(
        "ava", base_admin_url="postgresql://probe@127.0.0.1:1/postgres?connect_timeout=5"
    )
    return 0
sys.modules["cli.commands.lifecycle.start"] = types.SimpleNamespace(cmd_start=start)
sys.modules["cli.commands.lifecycle.root_driver"] = types.SimpleNamespace(complete_boot_start=lambda: None)
assert main.main(["init", "--serve-gateway", "--serve-agent-runner", "--machine-name", "probe"]) == 0
raise SystemExit(main.main(["start"]))
"""


def test_ava_start_writes_a_loguru_warning_to_stderr_and_its_log(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.update(AVA_HOME=str(home))
    child = subprocess.run(  # noqa: S603 — this interpreter, fixed code, a private home
        [sys.executable, "-I", "-B", "-c", _FIRST_START, str(_REPO_ROOT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert child.returncode == 0, child.stderr

    stderr = _ANSI.sub("", child.stderr)
    [warning] = [line for line in stderr.splitlines() if "[pgvector] pre-create skipped" in line]
    assert "WARNING" in warning and "OperationalError" not in warning
    assert "127.0.0.1" in warning  # the connection error itself, not a `%s` placeholder
    assert "service started" not in stderr

    records = [
        json.loads(line)
        for line in (home / "logs/cli-start.log").read_text().splitlines()
        if line.strip()
    ]
    assert any("[pgvector] pre-create skipped" in json.dumps(r) for r in records)
    assert not any("service_started" in json.dumps(r) for r in records)


def _dispatching(sink: list[str]) -> argparse.ArgumentParser:
    def dispatched(_args: argparse.Namespace) -> int:
        sink.append("dispatched")
        return 0

    parser = argparse.ArgumentParser(prog="ava")
    parser.add_argument("words", nargs="*")
    parser.set_defaults(func=dispatched)
    return parser


@pytest.mark.parametrize(
    ("argv", "name"),
    [
        (["restart"], "cli-restart"),
        (["lgtm", "on"], "cli-lgtm"),
        (["lgtm", "off"], "cli-lgtm"),
        (["status"], None),
        (["lgtm", "status"], None),
    ],
)
def test_only_the_verbs_that_bring_a_unit_up_open_sinks_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, cli_log_sinks: list[str], argv: list[str], name: str | None
) -> None:
    monkeypatch.setattr(
        parsers, "build_parser", _ignoring_retention(lambda: _dispatching(cli_log_sinks))
    )

    assert _main.main(argv) == 0

    assert cli_log_sinks == ([name, "dispatched"] if name else ["dispatched"])


def test_a_settings_failure_while_opening_sinks_keeps_its_actionable_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Opening the sinks builds Settings; a runner whose gateway is unreachable
    fails there with the same message the command itself would print."""
    import base.log
    from base.host.env.bootstrap import BootstrapFetchError

    def unreachable(
        *, name: str, producer: Callable[[], EventPipeline], machine_reader: Callable[[], str]
    ) -> None:
        raise BootstrapFetchError(f"could not fetch cluster config ({name})")

    dispatched: list[str] = []
    monkeypatch.setattr(base.log, "init_cli_process", unreachable)
    monkeypatch.setattr(
        parsers, "build_parser", _ignoring_retention(lambda: _dispatching(dispatched))
    )

    assert _main.main(["restart"]) == 1

    assert dispatched == []
    assert "could not fetch cluster config (cli-restart)" in capsys.readouterr().err


def _ignoring_retention[T](callback: Callable[[], T]) -> Callable[..., T]:
    def invoke(*_args: object, **_kwargs: object) -> T:
        return callback()

    return invoke


def test_main_reuses_only_the_explicit_caller_child_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    owners: list[list[subprocess.Popen[bytes]]] = []

    def dispatched(
        _args: argparse.Namespace, *, retained_children: list[subprocess.Popen[bytes]]
    ) -> int:
        owners.append(retained_children)
        return 0

    def stopped(*, retained_children: list[subprocess.Popen[bytes]], **_kwargs: object) -> int:
        owners.append(retained_children)
        return 0

    monkeypatch.setattr("cli.start_intent.run_start", dispatched)
    monkeypatch.setattr("cli.commands.lifecycle.stop.cmd_stop", stopped)
    shared: list[subprocess.Popen[bytes]] = []
    assert _main.main(["start"], retained_children=shared) == 0
    assert _main.main(["stop", "--yes"], retained_children=shared) == 0
    assert owners[0] is shared and owners[1] is shared
    assert _main.main(["start"]) == 0
    assert _main.main(["start"]) == 0
    assert owners[2] is not shared and owners[3] is not owners[2]


def test_inspection_parser_cannot_launch_without_a_child_owner() -> None:
    args = build_parser().parse_args(["start"])
    with pytest.raises(ValueError, match="caller-owned child retention"):
        args.func(args)


def test_logging_and_dispatch_borrow_the_same_operator_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    import base.log
    from base import telemetry

    pipeline = MagicMock(spec=telemetry.EventPipeline)
    factories: list[DatabaseFactory] = []
    inputs: list[object] = []

    def build(*, database: DatabaseFactory) -> telemetry.EventPipeline:
        factories.append(database)
        return pipeline

    def initialize(
        *, name: str, producer: Callable[[], EventPipeline], machine_reader: Callable[[], str]
    ) -> None:
        assert name == "cli-restart"
        assert callable(machine_reader)
        inputs.append(producer())

    def dispatch(args: argparse.Namespace) -> int:
        assert args.database_factory is factories[0]
        inputs.append(args.producer())
        return 0

    parser = argparse.ArgumentParser(prog="ava")
    parser.add_argument("words", nargs="*")
    parser.set_defaults(func=dispatch)
    monkeypatch.setattr(parsers, "build_parser", _ignoring_retention(lambda: parser))
    monkeypatch.setattr(telemetry, "build_pipeline", build)
    monkeypatch.setattr(base.log, "init_cli_process", initialize)
    assert _main.main(["restart"]) == 0
    assert len(factories) == 1
    assert inputs == [pipeline, pipeline]


def _parsed_args(args: argparse.Namespace) -> Callable[..., argparse.Namespace]:
    def parse(*_args: object, **_kwargs: object) -> argparse.Namespace:
        return args

    return parse


def _owned_pipeline(pipeline: telemetry.EventPipeline) -> Callable[..., telemetry.EventPipeline]:
    def build(**_kwargs: object) -> telemetry.EventPipeline:
        return pipeline

    return build


def _admitted_checkout(*_args: object) -> None:
    return None


def _cold_report(
    name: Literal["schedule", "stop", "dashboard"],
    args: argparse.Namespace,
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    if name == "schedule":
        from cli.commands.management import schedules_verify

        def failed_connect(_database: Database, **_kwargs: object) -> None:
            raise RuntimeError("schedule reader failed")

        monkeypatch.setattr(Database, "connect", failed_connect)
        args.check_file = args.rows_file = None
        args.no_notify = False
        return schedules_verify.h_schedules_verify(args)
    if name == "stop":
        from cli.commands.lifecycle.service_stop import report_postgres_stop_escalation

        notes: list[str] = []
        report_postgres_stop_escalation(
            postgres.Escalation(detail="fast shutdown timed out", killed=(42,)),
            notes,
            producer=args.producer,
        )
        assert len(notes) == 1 and "fast shutdown timed out" in notes[0]
        return 0
    from base.config import ConfigBoot, settings
    from base.telemetry.metrics import grafana_dashboard_supply
    from cli.commands.converge.spec import ConvergeCtx
    from cli.commands.observability import lgtm_native

    original = RuntimeError("dashboard reader failed")

    def failed_render(database: Database) -> tuple[str, list[str]]:
        raise original

    monkeypatch.setattr(grafana_dashboard_supply, "render_dashboard_json", failed_render)
    repo = _REPO_ROOT
    tag = lgtm_native.platform_tag()
    if tag is None:
        pytest.skip("native LGTM is unsupported on this platform")
    native = path / "lgtm/native"
    native.mkdir(parents=True)
    versions = yaml.safe_load((repo / "deploy/lgtm/native/versions.yml").read_text())
    (native / "version-prometheus").write_text(versions["prometheus"]["version"])
    (native / "platform-prometheus").write_text(tag)
    monkeypatch.setattr(settings.observability, "lgtm_storage_dir", str(path / "storage"))
    destination = native / "config/provisioning/dashboards/ava-ops-main.json"
    destination.parent.mkdir(parents=True)
    destination.write_text("previous dashboard")
    context = ConvergeCtx(
        repo=repo,
        ava_home=path,
        roles=frozenset({"observability-station"}),
        config=ConfigBoot(),
        database_factory=args.database_factory,
        producer=args.producer,
        services=frozenset({"prometheus"}),
    )
    lgtm_native.ensure_lgtm_native_step(context)
    return 0


@pytest.mark.parametrize(
    ("name", "event", "result"),
    [
        ("schedule", "schedule_verify_failed", 2),
        ("stop", "postgres_stop_escalated", 0),
        ("dashboard", "lgtm_dashboard_render_failed", 0),
    ],
)
def test_cold_reporter_delivers_through_main_owned_pipeline(
    name: Literal["schedule", "stop", "dashboard"],
    event: str,
    result: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[telemetry.Event] = []
    gates: list[ProcessDbGate] = []
    pipelines: list[telemetry.EventPipeline] = []
    factories: list[DatabaseFactory] = []

    # Handles are never dialed; the same real operator gate reaches work and writer.
    from base.db.config import db_config_from_settings

    def construct(*, gate: ProcessDbGate) -> Database:
        gates.append(gate)
        return Database(db_config_from_settings(), gate=gate)

    def build(*, database: DatabaseFactory) -> telemetry.EventPipeline:
        factories.append(database)

        def write(batch: list[telemetry.Event]) -> None:
            database()
            events.extend(batch)

        pipeline = telemetry.EventPipeline(writer=write)
        pipelines.append(pipeline)
        return pipeline

    def dispatch(args: argparse.Namespace) -> int:
        args.database_factory()
        return _cold_report(name, args, tmp_path, monkeypatch)

    def forbidden_binding(**_kwargs: object) -> None:
        raise AssertionError("cold reporters must use their explicit entry owner")

    args = argparse.Namespace(func=dispatch)
    monkeypatch.setattr(telemetry, "init_telemetry", forbidden_binding)
    monkeypatch.setattr(Database, "from_settings", construct)
    monkeypatch.setattr(telemetry, "build_pipeline", build)
    monkeypatch.setattr(parsers, "parse_args", _parsed_args(args))
    monkeypatch.setattr("cli.preflight.require_own_checkout", _admitted_checkout)
    assert _main.main(["status"]) == result
    if name == "dashboard":
        destination = tmp_path / "lgtm/native/config/provisioning/dashboards/ava-ops-main.json"
        assert destination.read_text() == "previous dashboard"
    assert factories == [args.database_factory]
    assert len(pipelines) == 1 and pipelines[0].stopped
    assert [record.event_name for record in events] == [event]
    assert len(gates) >= 2 and all(gate is gates[0] for gate in gates)


def test_quiet_command_does_not_construct_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(**kwargs: object) -> telemetry.EventPipeline:
        raise AssertionError("a quiet command must not construct a writer")

    monkeypatch.setattr(telemetry, "build_pipeline", forbidden)
    owner = operator_event_pipeline(operator_database_factory())
    assert owner.close() is None

    def quiet(_args: argparse.Namespace) -> int:
        return 0

    args = argparse.Namespace(func=quiet)
    monkeypatch.setattr(parsers, "parse_args", _parsed_args(args))
    monkeypatch.setattr("cli.preflight.require_own_checkout", _admitted_checkout)
    assert _main.main(["status"]) == 0


def test_unfinished_command_writer_remains_owned_until_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def write(batch: list[telemetry.Event]) -> None:
        entered.set()
        assert release.wait(5)

    pipeline = telemetry.EventPipeline(writer=write, batch_size=1)
    monkeypatch.setattr(telemetry, "build_pipeline", _owned_pipeline(pipeline))
    owner = operator_event_pipeline(operator_database_factory())
    try:
        telemetry.emit("telemetry", "schedule_verify_failed", producer=owner)
        assert entered.wait(5)
        receipt = owner.close(timeout=0)
        assert receipt is not None
        assert receipt.status is telemetry.DrainStatus.UNFINISHED
        assert owner() is pipeline and not pipeline.stopped
    finally:
        release.set()
        receipt = owner.close()
        assert receipt is not None
        assert receipt.status is telemetry.DrainStatus.COMPLETED


@pytest.mark.parametrize("body_fails", [False, True])
def test_command_close_preserves_original_writer_error_and_primary(
    body_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = ValueError("command failed")
    cleanup = RuntimeError("writer failed")

    def write(batch: list[telemetry.Event]) -> None:
        raise cleanup

    pipeline = telemetry.EventPipeline(writer=write)
    monkeypatch.setattr(telemetry, "build_pipeline", _owned_pipeline(pipeline))

    def dispatch(args: argparse.Namespace) -> int:
        telemetry.emit("telemetry", "schedule_verify_failed", producer=args.producer)
        if body_fails:
            raise primary
        return 0

    args = argparse.Namespace(func=dispatch)
    monkeypatch.setattr(parsers, "parse_args", _parsed_args(args))
    monkeypatch.setattr("cli.preflight.require_own_checkout", _admitted_checkout)
    with pytest.raises(ValueError if body_fails else RuntimeError) as caught:
        _main.main(["status"])
    assert caught.value is (primary if body_fails else cleanup)
    assert pipeline.stopped
    with pytest.raises(RuntimeError) as repeated:
        args.producer.close(timeout=0)
    assert repeated.value is cleanup
    if body_fails:
        assert any("writer failed" in note for note in primary.__notes__)
