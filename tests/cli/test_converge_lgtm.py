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

from cli.commands import _lgtm
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
    assert len(calls) == 1
    assert calls[0][0][-2:] == ["bash", "start.sh"]
    assert calls[0][1] == ctx.repo / "deploy" / "lgtm"


def test_hybrid_gateway_runner_marker_runs_start_sh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = _ctx(tmp_path)
    ctx = ConvergeCtx(
        repo=base.repo,
        ava_home=base.ava_home,
        roles=frozenset({"gateway", "agent-runner"}),
    )
    ctx.ava_home.mkdir(parents=True)
    (ctx.ava_home / "lgtm-host").touch()
    monkeypatch.setattr(_lgtm.shutil, "which", lambda _name: "/usr/local/bin/docker")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    class _Result:
        returncode = 0

    calls: list[list[str]] = []
    monkeypatch.setattr(
        _lgtm.subprocess,
        "run",
        lambda cmd, **_kw: calls.append(cmd) or _Result(),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    )

    _lgtm.ensure_lgtm_stack_step(ctx)
    assert len(calls) == 1
    assert calls[0][-2:] == ["bash", "start.sh"]


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
    """Remote runners enter through the authenticated gateway collector;
    unauthenticated Tempo/Loki/Prometheus APIs must never become off-box."""
    compose_path = Path(__file__).resolve().parents[2] / "deploy/lgtm/docker-compose.yml"
    text = compose_path.read_text(encoding="utf-8")
    assert "LGTM_BIND_HOST" not in text
    compose = yaml.safe_load(text)
    services = compose["services"]
    assert services["tempo"]["ports"] == ["127.0.0.1:3200:3200", "127.0.0.1:14318:4318"]
    assert services["loki"]["ports"] == ["127.0.0.1:3100:3100"]
    assert services["prometheus"]["ports"] == ["127.0.0.1:9090:9090"]
    assert services["grafana"]["ports"] == ["127.0.0.1:3003:3000"]


def test_grafana_requires_gateway_auth_proxy() -> None:
    """Grafana holds no cluster secret and has no direct anonymous/login path.

    Local processes are inside the machine boundary and can assert an auth
    proxy header; the externally reachable gateway is responsible for
    stripping spoofed identity and injecting only the fixed Viewer. Grafana
    must not mint a second browser session cookie.
    """
    compose_path = Path(__file__).resolve().parents[2] / "deploy/lgtm/docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    env = compose["services"]["grafana"]["environment"]
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert env["GF_AUTH_PROXY_ENABLED"] == "true"
    assert env["GF_AUTH_PROXY_HEADER_NAME"] == "X-Ava-Grafana-User"
    assert env["GF_AUTH_PROXY_AUTO_SIGN_UP"] == "true"
    assert env["GF_AUTH_PROXY_ENABLE_LOGIN_TOKEN"] == "false"  # noqa: S105
    assert env["GF_USERS_AUTO_ASSIGN_ORG_ROLE"] == "Viewer"
    assert env["GF_USERS_ALLOW_SIGN_UP"] == "false"
    assert env["GF_AUTH_BASIC_ENABLED"] == "false"
    # GF_AUTH_DISABLE_LOGIN must stay unset: Grafana's auth-proxy auto-signup
    # depends on the internal Grafana proxy client that this flag removes.
    assert "GF_AUTH_DISABLE_LOGIN" not in env
    assert env["GF_AUTH_DISABLE_LOGIN_FORM"] == "true"
    assert env["GF_AUTH_DISABLE_SIGNOUT_MENU"] == "true"
    assert "AVA_CLUSTER_SECRET" not in env


def test_grafana_root_url_has_no_direct_port_fallback() -> None:
    """Grafana's browser URL is supplied by lifecycle as gateway + /grafana/;
    compose must never fall back to a second public :3003 address."""
    compose_path = Path(__file__).resolve().parents[2] / "deploy/lgtm/docker-compose.yml"
    text = compose_path.read_text(encoding="utf-8")
    compose = yaml.safe_load(text)
    root = compose["services"]["grafana"]["environment"]["GF_SERVER_ROOT_URL"]
    assert root == "${GRAFANA_ROOT_URL:?gateway-derived GRAFANA_ROOT_URL is required}"
    assert "http://localhost:3003" not in text


def test_pure_runner_marker_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """LGTM must be co-located with a gateway; a stale marker on a pure runner
    is a configuration error, not permission to start a second backend."""
    ctx = _ctx(tmp_path)
    ctx = ConvergeCtx(repo=ctx.repo, ava_home=ctx.ava_home, roles=frozenset({"agent-runner"}))
    ctx.ava_home.mkdir(parents=True)
    (ctx.ava_home / "lgtm-host").touch()
    monkeypatch.setattr(_lgtm.shutil, "which", lambda _name: "/usr/local/bin/docker")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    with pytest.raises(RuntimeError, match="requires the gateway capability"):
        _lgtm.ensure_lgtm_stack_step(ctx)
