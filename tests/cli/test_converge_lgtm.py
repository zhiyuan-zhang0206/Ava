"""cli.commands._lgtm — converge bring-up gating tests.

The LGTM compose stack is a host singleton; converge runs on every `ava start`
of every cluster on the box, so the bring-up must fire ONLY on the home
carrying the $AVA_HOME/lgtm-host marker. No docker: subprocess is
monkeypatched; the gating is the logic under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cli.commands import _lgtm, _lgtm_native
from cli.commands._converge_spec import ConvergeCtx


def _ctx(tmp_path: Path) -> ConvergeCtx:
    repo = tmp_path / "repo"
    (repo / "deploy" / "lgtm").mkdir(parents=True)
    return ConvergeCtx(repo=repo, ava_home=tmp_path / "home", roles=frozenset({"gateway"}))


def test_no_marker_never_touches_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A home without the lgtm-host marker (every dev worktree cluster) is a
    no-op — it must not recreate the host's containers from its own checkout."""
    ctx = _ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    runs: list[list[str]] = []
    monkeypatch.setattr(_lgtm.subprocess, "run", lambda cmd, **_kw: runs.append(cmd))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    _lgtm.ensure_lgtm_stack_step(ctx)
    assert runs == []


def test_marker_runs_start_sh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Marker + docker present -> the idempotent start.sh runs in deploy/lgtm."""
    ctx = _ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    (ctx.ava_home / "lgtm-host").touch()
    monkeypatch.setattr(_lgtm.shutil, "which", lambda _name: "/usr/local/bin/docker")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    calls: list[tuple[list[str], Path]] = []

    class _Result:
        returncode = 0

    def fake_run(cmd: list[str], **kw: object) -> _Result:
        calls.append((cmd, Path(str(kw["cwd"]))))
        return _Result()

    monkeypatch.setattr(_lgtm.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]

    _lgtm.ensure_lgtm_stack_step(ctx)
    assert calls == [(["bash", "start.sh"], ctx.repo / "deploy" / "lgtm")]


def test_marker_without_docker_warns_and_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A marked host missing the docker CLI is an environment limit: warn +
    skip (browser-step contract), never raise — converge must proceed."""
    ctx = _ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    (ctx.ava_home / "lgtm-host").touch()
    monkeypatch.setattr(_lgtm.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    runs: list[list[str]] = []
    monkeypatch.setattr(_lgtm.subprocess, "run", lambda cmd, **_kw: runs.append(cmd))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    _lgtm.ensure_lgtm_stack_step(ctx)
    assert runs == []
    assert "docker CLI not found" in capsys.readouterr().err


def test_failing_start_sh_propagates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The marker is the operator's statement that this host owns the
    observability backend — a failing bring-up is a real defect, not a skip."""
    ctx = _ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    (ctx.ava_home / "lgtm-host").touch()
    monkeypatch.setattr(_lgtm.shutil, "which", lambda _name: "/usr/local/bin/docker")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    class _Failed:
        returncode = 1

    monkeypatch.setattr(_lgtm.subprocess, "run", lambda *_a, **_kw: _Failed())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    with pytest.raises(RuntimeError, match=r"start\.sh exited 1"):
        _lgtm.ensure_lgtm_stack_step(ctx)


def test_backend_ports_are_unconditionally_loopback_only() -> None:
    """Native and container backend listeners stay pinned to loopback."""
    compose_path = Path(__file__).resolve().parents[2] / "deploy/lgtm/docker-compose.yml"
    text = compose_path.read_text(encoding="utf-8")
    assert "LGTM_BIND_HOST" not in text
    compose = yaml.safe_load(text)
    services = compose["services"]
    assert services["tempo"]["ports"] == ["127.0.0.1:3200:3200", "127.0.0.1:14318:4318"]
    assert services["grafana"]["ports"] == ["3003:3000"]
    native = compose_path.parent / "native"
    loki = (native / "config/loki.yaml").read_text(encoding="utf-8")
    assert "http_listen_address: 127.0.0.1" in loki
    assert "grpc_listen_address: 127.0.0.1" in loki
    assert "instance_addr: 127.0.0.1" in loki
    assert (
        "--web.listen-address=127.0.0.1:9090"
        in _lgtm_native._NATIVE_CONSTANTS["prometheus"].arguments
    )


def test_native_step_runs_only_for_the_marker_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _ctx(tmp_path)
    ctx.ava_home.mkdir(parents=True)
    (ctx.ava_home / "lgtm-host").touch()
    calls: list[tuple[Path, Path]] = []

    def record_ensure(repo: Path, home: Path) -> None:
        calls.append((repo, home))

    monkeypatch.setattr(
        _lgtm_native,
        "ensure_lgtm_native",
        record_ensure,
    )

    _lgtm_native.ensure_lgtm_native_step(ctx)

    assert calls == [(ctx.repo, ctx.ava_home)]
