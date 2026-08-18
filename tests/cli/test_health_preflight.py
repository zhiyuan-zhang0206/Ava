"""Health preflight — the start-time data-plane + checkout self-check (#607).

Extends the port-conflict preflight (#1205): `ava start` / `ava converge` now
also probes the data plane (pg/redis, the URLs the runtime dials) and the
checkout (HEAD vs cluster pin, dirty marker). Same contract as the port
preflight — warning-only, logged to `$AVA_HOME/logs/health_preflight.log`,
never failing a start.
"""

import subprocess
from pathlib import Path

import pytest

from cli.commands import _health_preflight as _hp
from cli.commands._converge_spec import ConvergeCtx


class _FakeDataPlane:
    db_url = "postgresql://ava:sek@127.0.0.1:5433/ava"
    redis_url = "redis://:sek@127.0.0.1:6380/0"


class _FakeSettings:
    data_plane = _FakeDataPlane()


def _preflight_ctx(tmp_path: Path, roles: frozenset | None = frozenset({"gateway"})) -> ConvergeCtx:
    return ConvergeCtx(repo=tmp_path / "repo", ava_home=tmp_path / "home", roles=roles)  # pyright: ignore[reportUnknownArgumentType]


def _install_fake_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_hp, "settings", _FakeSettings())


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
    """Probe seams pointing at a healthy plane + an idle update, recording calls."""
    calls: dict[str, list[str]] = {"pg": [], "redis": []}

    def _pg(url: str) -> str | None:
        calls["pg"].append(url)
        return None

    def _redis(url: str) -> str | None:
        calls["redis"].append(url)
        return None

    monkeypatch.setattr(_hp, "probe_postgres", _pg)
    monkeypatch.setattr(_hp, "probe_redis", _redis)
    monkeypatch.setattr(_hp, "_update_in_flight", lambda: False)
    return calls


# ─── the converge step: warn + log, never block ────────────────────────────


def test_health_preflight_warns_and_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    """A data-plane / checkout finding → the start CONTINUES (rc-free step) but
    prints the warning and appends it to $AVA_HOME/logs/health_preflight.log."""
    ctx = _preflight_ctx(tmp_path)
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """No findings → no output, no log file."""
    ctx = _preflight_ctx(tmp_path)
    monkeypatch.setattr(_hp, "collect_health_warnings", lambda _ctx: [])  # pyright: ignore[reportUnknownArgumentType]

    _hp.ensure_health_preflight(ctx)

    assert capsys.readouterr().err == ""  # pyright: ignore[reportUnknownMemberType]
    assert not (tmp_path / "home" / "logs" / "health_preflight.log").exists()


def test_health_preflight_never_fails_start_on_scan_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """A scan exception prints a notice and returns — the step must not turn a
    warning pass into a failed start."""
    ctx = _preflight_ctx(tmp_path)
    monkeypatch.setattr(
        _hp,
        "collect_health_warnings",
        lambda _ctx: (_ for _ in ()).throw(RuntimeError("boom")),  # pyright: ignore[reportUnknownArgumentType]
    )

    _hp.ensure_health_preflight(ctx)

    assert "health preflight skipped: boom" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


# ─── data plane: role-aware probing ────────────────────────────────────────


def test_data_plane_gateway_cold_start_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _upstream
):
    """Gateway with neither pg nor redis bound → nothing probed, no warnings: the
    start sequence brings the instance up right after converge, so probing now
    would warn on every boot."""
    _install_fake_settings(monkeypatch)
    ctx = _preflight_ctx(tmp_path)
    monkeypatch.setattr(
        _hp,
        "expected_cluster_ports",
        lambda _home: {"postgres": _free_port(), "redis": _free_port()},  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _hp._data_plane_warnings(ctx) == []
    assert _upstream["pg"] == [] and _upstream["redis"] == []


def test_data_plane_gateway_partial_up_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _upstream
):
    """Gateway with one of the two bound → probes, and a failed probe warns."""
    import socket

    _install_fake_settings(monkeypatch)
    ctx = _preflight_ctx(tmp_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    held = sock.getsockname()[1]
    try:
        monkeypatch.setattr(
            _hp,
            "expected_cluster_ports",
            lambda _home: {"postgres": held, "redis": _free_port()},  # pyright: ignore[reportUnknownArgumentType]
        )
        # The pg probe is replaced to fail; the redis probe stays the recorder,
        # proving both were dialed on a partially-up plane.
        monkeypatch.setattr(_hp, "probe_postgres", lambda _url: "Connection refused")  # pyright: ignore[reportUnknownArgumentType]

        warnings = _hp._data_plane_warnings(ctx)
        assert any(w.startswith("postgres") and "Connection refused" in w for w in warnings)
        assert _upstream["redis"] == [_FakeSettings.data_plane.redis_url]
    finally:
        sock.close()


def test_data_plane_runner_probes_unconditionally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _upstream
):
    """A runner's plane is remote — an unreachable one is the real signal, so even
    a fully cold host probes (the runner has no local bring-up to fix it)."""
    _install_fake_settings(monkeypatch)
    ctx = _preflight_ctx(tmp_path, roles=frozenset({"agent-runner"}))

    assert _hp._data_plane_warnings(ctx) == []  # probes pass → silent
    assert _upstream["pg"] == [_FakeSettings.data_plane.db_url]
    assert _upstream["redis"] == [_FakeSettings.data_plane.redis_url]


def test_data_plane_unconfigured_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _upstream
):
    """roles=None (unit not configured yet) → nothing probed."""
    ctx = _preflight_ctx(tmp_path, roles=None)

    assert _hp._data_plane_warnings(ctx) == []
    assert _upstream["pg"] == [] and _upstream["redis"] == []


def test_probe_postgres_reports_refused():
    """A dead endpoint yields an error string, not an exception. (The exact
    wording differs by platform — "Connection refused" on Linux, "Connection
    reset" on macOS — so only the failure is asserted.)"""
    err = _hp.probe_postgres(f"postgresql://u:p@127.0.0.1:{_free_port()}/db")
    assert err is not None and "connection" in err.lower()


def test_probe_redis_reports_refused():
    """A dead endpoint yields an error string, not an exception. (Wording varies
    by platform — see probe_postgres's twin.)"""
    err = _hp.probe_redis(f"redis://127.0.0.1:{_free_port()}/0")
    assert err is not None and "connection" in err.lower()


def test_redact_strips_userinfo():
    assert _hp._redact("postgresql://user:sekret@127.0.0.1:5433/db") == (
        "postgresql://127.0.0.1:5433/db"
    )


# ─── checkout: HEAD vs pin + dirty, prod installs only ─────────────────────


def _git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ConvergeCtx, str, str]:
    """A real git repo with two commits → (ctx, head_sha, first_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S603 — test-controlled git argv
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
        subprocess.run(  # noqa: S603 — test-controlled git argv
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
    ctx = ConvergeCtx(repo=repo, ava_home=tmp_path / "home", roles=frozenset({"gateway"}))
    monkeypatch.setattr(_hp, "_update_in_flight", lambda: False)
    return ctx, head, first


def test_checkout_dirty_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Uncommitted changes in the source tree → a warning naming the count."""
    ctx, _head, _first = _git_repo(tmp_path, monkeypatch)
    (tmp_path / "repo" / "stray.txt").write_text("uncommitted\n")

    warnings = _hp._checkout_warnings(ctx)

    assert any("dirty (1 changed file" in w for w in warnings)


def test_checkout_clean_and_aligned_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Clean tree + HEAD == pin → no warnings."""
    ctx, head, _first = _git_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(_hp, "_cluster_pin", lambda: head)

    assert _hp._checkout_warnings(ctx) == []


def test_checkout_pin_relation_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """HEAD ahead of the pin (stray git pull) → a warning naming the relation."""
    ctx, _head, first = _git_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(_hp, "_cluster_pin", lambda: first)

    warnings = _hp._checkout_warnings(ctx)

    assert any("ahead of the cluster pin" in w for w in warnings)


def test_checkout_pin_unknown_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No pin (never rolled out) → no pin line, but dirty still reports."""
    ctx, _head, _first = _git_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(_hp, "_cluster_pin", lambda: None)

    assert not any("cluster pin" in w for w in _hp._checkout_warnings(ctx))


def test_checkout_worktree_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A dev worktree path → checkout state is skipped entirely (dev context)."""
    wt = tmp_path / ".claude" / "worktrees" / "some-task"
    wt.mkdir(parents=True)
    ctx = ConvergeCtx(repo=wt, ava_home=tmp_path / "home", roles=frozenset({"gateway"}))
    (wt / "stray.txt").write_text("dirty\n")

    assert _hp._checkout_warnings(ctx) == []
