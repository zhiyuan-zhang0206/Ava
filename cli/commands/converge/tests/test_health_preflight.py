"""Health preflight — the start-time data-plane + checkout self-check (#607).

`ava start` / `ava converge` probe the data plane (pg/redis, the URLs the runtime
dials) and the checkout (dirty marker). Warning-only, logged to
`$AVA_HOME/logs/health_preflight.log`, never failing a start.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from base.agents.context.clients import DatabaseFactory
from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import MachineRoles
from base.host.system.tests.fake_commands import fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge import health_preflight as _hp
from cli.commands.converge.spec import ConvergeCtx
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

_PG_URL = "postgresql://ava:sek@127.0.0.1:5433/ava"
_REDIS_URL = "redis://:sek@127.0.0.1:6380/0"


def _preflight_ctx(
    tmp_path: Path,
    roles: MachineRoles | None = frozenset({"gateway"}),
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> ConvergeCtx:
    config = command_runtime.config
    config.set_field("db_url", _PG_URL)
    config.set_field("redis_url", _REDIS_URL)
    return ConvergeCtx(
        repo=tmp_path / "repo",
        ava_home=tmp_path / "home",
        roles=roles,
        config=config,
        database_factory=operator_database,
        producer=producer,
        machine=command_runtime.machine,
        helper=refuse_helper,
        host=fake_host(),
        services=frozenset(),
        timers=ManualTimers(),
    )


def _free_port() -> int:
    """An unbound port (bind port 0, note it, release)."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def _upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Probe seams pointing at a healthy plane, recording calls."""
    calls: dict[str, list[str]] = {"pg": [], "redis": []}

    def _pg(url: str) -> str | None:
        calls["pg"].append(url)
        return None

    def _redis(url: str) -> str | None:
        calls["redis"].append(url)
        return None

    monkeypatch.setattr(_hp, "probe_postgres", _pg)
    monkeypatch.setattr(_hp, "probe_redis", _redis)
    return calls


# ─── the converge step: warn + log, never block ────────────────────────────


def test_health_preflight_warns_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    """A data-plane / checkout finding → the start CONTINUES (rc-free step) but
    prints the warning and appends it to $AVA_HOME/logs/health_preflight.log."""
    ctx = _preflight_ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    monkeypatch.setattr(
        _hp,
        "collect_health_warnings",
        lambda _ctx: ["postgres …: Connection refused"],  # pyright: ignore[reportUnknownArgumentType]
    )

    _hp.ensure_health_preflight(ctx)  # must not raise — a preflight never fails a start

    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "HEALTH PREFLIGHT" in err and "Connection refused" in err
    log = tmp_path / "home" / "logs" / "health_preflight.log"
    assert log.exists()
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and lines[0].endswith("postgres …: Connection refused")


def test_health_preflight_silent_when_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    """No findings → no output, no log file."""
    ctx = _preflight_ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    monkeypatch.setattr(_hp, "collect_health_warnings", lambda _ctx: [])  # pyright: ignore[reportUnknownArgumentType]

    _hp.ensure_health_preflight(ctx)

    assert capsys.readouterr().err == ""  # pyright: ignore[reportUnknownMemberType]
    assert not (tmp_path / "home" / "logs" / "health_preflight.log").exists()


def test_health_preflight_never_fails_start_on_scan_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    """A scan exception prints a notice and returns — the step must not turn a
    warning pass into a failed start."""
    ctx = _preflight_ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    monkeypatch.setattr(
        _hp,
        "collect_health_warnings",
        lambda _ctx: (_ for _ in ()).throw(RuntimeError("boom")),  # pyright: ignore[reportUnknownArgumentType]
    )

    _hp.ensure_health_preflight(ctx)

    assert "health preflight skipped: boom" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


# ─── data plane: role-aware probing ────────────────────────────────────────


def test_data_plane_gateway_cold_start_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _upstream,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    """Gateway with neither pg nor redis bound → nothing probed, no warnings: the
    start sequence brings the instance up right after converge, so probing now
    would warn on every boot."""
    ctx = _preflight_ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    monkeypatch.setattr(
        _hp,
        "get_record",
        lambda _home: SimpleNamespace(ports={"postgres": _free_port(), "redis": _free_port()}),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _hp.data_plane_warnings(ctx) == []
    assert _upstream["pg"] == [] and _upstream["redis"] == []


def test_data_plane_gateway_partial_up_probes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _upstream,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    """Gateway with one of the two bound → probes, and a failed probe warns."""
    import socket

    ctx = _preflight_ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    held = sock.getsockname()[1]
    try:
        monkeypatch.setattr(
            _hp,
            "get_record",
            lambda _home: SimpleNamespace(ports={"postgres": held, "redis": _free_port()}),  # pyright: ignore[reportUnknownArgumentType]
        )
        # The pg probe is replaced to fail; the redis probe stays the recorder,
        # proving both were dialed on a partially-up plane.
        monkeypatch.setattr(_hp, "probe_postgres", lambda _url: "Connection refused")  # pyright: ignore[reportUnknownArgumentType]

        warnings = _hp.data_plane_warnings(ctx)
        assert any(w.startswith("postgres") and "Connection refused" in w for w in warnings)
        assert _upstream["redis"] == [_REDIS_URL]
    finally:
        sock.close()


def test_data_plane_runner_probes_unconditionally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _upstream,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    """A runner's plane is remote — an unreachable one is the real signal, so even
    a fully cold host probes (the runner has no local bring-up to fix it)."""
    ctx = _preflight_ctx(
        tmp_path,
        roles=frozenset({"agent-runner"}),
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    assert _hp.data_plane_warnings(ctx) == []  # probes pass → silent
    assert _upstream["pg"] == [_PG_URL]
    assert _upstream["redis"] == [_REDIS_URL]


def test_data_plane_unconfigured_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _upstream,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    """roles=None (unit not configured yet) → nothing probed."""
    ctx = _preflight_ctx(
        tmp_path,
        roles=None,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    was_prepared = ctx.config.prepared
    assert _hp.data_plane_warnings(ctx) == []
    assert _upstream["pg"] == [] and _upstream["redis"] == []
    assert ctx.config.prepared == was_prepared


def test_preflight_keeps_reading_its_owners_live_urls(
    tmp_path: Path,
    _upstream: dict[str, list[str]],
    operator_database: DatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> None:
    ctx = _preflight_ctx(
        tmp_path,
        roles=frozenset({"agent-runner"}),
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    assert _hp.data_plane_warnings(ctx) == []
    changed_pg = "postgresql://ava:changed@127.0.0.1:5434/other"
    changed_redis = "redis://:changed@127.0.0.1:6381/1"
    ctx.config.set_field("db_url", changed_pg)
    ctx.config.set_field("redis_url", changed_redis)
    assert _hp.data_plane_warnings(ctx) == []
    assert _upstream["pg"] == [_PG_URL, changed_pg]
    assert _upstream["redis"] == [_REDIS_URL, changed_redis]


def test_probe_postgres_reports_refused(postgres_access: None):
    """A dead endpoint yields an error string, not an exception. (The exact
    wording differs by platform — "Connection refused" on Linux, "Connection
    reset" on macOS — so only the failure is asserted.)"""
    err = _hp.probe_postgres(f"postgresql://u:p@127.0.0.1:{_free_port()}/db")
    assert err is not None and "connection" in err.lower()


def test_probe_redis_reports_refused(redis_access: None):
    """A dead endpoint yields an error string, not an exception. (Wording varies
    by platform — see probe_postgres's twin.)"""
    err = _hp.probe_redis(f"redis://127.0.0.1:{_free_port()}/0")
    assert err is not None and "connection" in err.lower()


def test_redact_strips_userinfo():
    assert _hp.redact("postgresql://user:sekret@127.0.0.1:5433/db") == (
        "postgresql://127.0.0.1:5433/db"
    )


# ─── checkout: dirty marker, prod installs only ─────────────────────────────


def _git_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> tuple[ConvergeCtx, str, str]:
    """A real git repo with two commits → (ctx, head_sha, first_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def _commit(msg: str) -> str:
        (repo / "f.txt").write_text(msg + "\n")
        subprocess.run(
            ["git", "add", "f.txt"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", msg],
            cwd=str(repo),
            check=True,
            capture_output=True,
            env=env,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

    first = _commit("one")
    head = _commit("two")
    ctx = ConvergeCtx(
        repo=repo,
        ava_home=tmp_path / "home",
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
    return ctx, head, first


def test_checkout_dirty_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    """Uncommitted changes in the source tree → a warning naming the count."""
    ctx, _head, _first = _git_repo(
        tmp_path,
        monkeypatch,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    (tmp_path / "repo" / "stray.txt").write_text("uncommitted\n")

    warnings = _hp.checkout_warnings(ctx)

    assert any("dirty (1 changed file" in w for w in warnings)


def test_checkout_clean_is_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    """A clean checkout at any commit warns nothing: there is no cluster target
    commit to compare its HEAD against."""
    ctx, _head, _first = _git_repo(
        tmp_path,
        monkeypatch,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    assert _hp.checkout_warnings(ctx) == []


def test_checkout_worktree_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    """A dev worktree path → checkout state is skipped entirely (dev context)."""
    wt = tmp_path / ".claude" / "worktrees" / "some-task"
    wt.mkdir(parents=True)
    ctx = ConvergeCtx(
        repo=wt,
        ava_home=tmp_path / "home",
        roles=frozenset({"gateway"}),
        config=command_runtime.config,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
        helper=refuse_helper,
        host=fake_host(),
        services=frozenset(),
        timers=ManualTimers(),
    )
    (wt / "stray.txt").write_text("dirty\n")

    assert _hp.checkout_warnings(ctx) == []
