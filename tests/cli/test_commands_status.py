"""Gateway status, source drift, cluster pin, and host readings; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import cast

import pytest

from cli import commands as _cli
from shared.config import settings
from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import _FakeResponse, _FakeResult, _patch_gateway_http, _sess
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base


@pytest.fixture(autouse=True)
def _local_status_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Status tests do not initialize a cluster or contact any live gateway."""
    import httpx

    from services.ava_root.client import RootClientError

    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))

    def _unreachable(*_args: object, **_kwargs: object) -> None:
        raise httpx.ConnectError("isolated status test")

    class _AbsentRoot:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def status(self) -> object:
            raise RootClientError("isolated status test")

    monkeypatch.setattr(httpx, "get", _unreachable)
    monkeypatch.setattr("services.ava_root.client.RootClient", _AbsentRoot)


# ─── gateway cluster-status probe carries the bearer ───────────────────────────


def test_status_explains_persistently_unselected_services(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from shared import service_selection

    monkeypatch.setattr(
        service_selection,
        "read_selection",
        lambda: service_selection.ServiceSelection("only", frozenset({"gateway"})),
    )
    assert _cli.cmd_status() == 0
    frontend = next(line for line in capsys.readouterr().out.splitlines() if "frontend" in line)
    assert "disabled by desired service set" in frontend


def test_fetch_gateway_cluster_status_sends_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `ava status` cluster-status probe presents the cluster-secret bearer, so
    an authenticated-but-healthy gateway reads as up instead of a false 401."""
    import httpx

    from cli.commands.cluster import _fetch_gateway_cluster_status

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "s3cr3t")
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"ok": True}

    def _fake_get(url: str, *, timeout: float, headers: dict[str, str]) -> _Resp:
        captured["url"] = url
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    assert _fetch_gateway_cluster_status() == {"ok": True}
    assert captured["url"] == "http://gw:8000/api/cluster/status"
    assert captured["headers"] == {"Authorization": "Bearer s3cr3t"}


def test_fetch_gateway_cluster_status_no_bearer_when_secret_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unprovisioned (no secret): send no auth header rather than a blank bearer."""
    import httpx

    from cli.commands.cluster import _fetch_gateway_cluster_status

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {}

    def _fake_get(url: str, *, timeout: float, headers: dict[str, str]) -> _Resp:
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    _fetch_gateway_cluster_status()
    assert captured["headers"] == {}


# ─── status ───────────────────────────────────────────────────────────────────


def test_status_runs_without_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """status can output normally even when all command mocks return non-0 (empty cluster) (no raise)."""
    _ = capsys
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(_cli.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("gateway") in out
    assert _sess("frontend") in out


def test_status_gateway_excludes_ops(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(_cli.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("gateway") in out
    assert _sess("ops") not in out


def test_status_agent_runner_shows_ops(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("ops") in out
    assert _sess("gateway") not in out


def test_status_shows_browser_skip_reason(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Issue #1111: an enabled-but-incapable ava-browser is shown WITH its reason
    rather than silently dropped, so `ava status` (the first diagnostic command)
    is not blind to the broken service."""
    from cli.commands import _repo

    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_repo.settings.services, "browser_enabled", True)
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: "no display (headless)")
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(_cli.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("browser") in out
    assert "skipped: no display" in out


def test_status_shows_the_gate_entry_row(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The entry service shares the root/identity readiness table with the app."""
    import cli.commands._probe as probe_module
    import cli.commands.status as status_module
    from cli.commands._probe import ServiceProbe

    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(status_module, "_root_tree_units", lambda: {"gate": {"state": "running"}})
    monkeypatch.setattr(
        probe_module,
        "_probe_service",
        lambda _spec: ServiceProbe(False, "down", "owned listener not ready"),
    )
    assert _cli.cmd_status() == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith(_sess("gate") + " "))
    assert "✓" in row and "✗" in row
    assert "owned listener not ready" in row
    assert "gate (fleet UI entry):" not in out


def test_status_shows_the_end_to_end_redis_bridge_row(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """The host-level relay must not disappear behind healthy service rows."""
    import cli.commands.status as status_mod

    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        status_mod,
        "print_redis_bridge_status",
        lambda: sys.stdout.write("  ✗ 10.64.0.7:6380 Redis PING: connection refused\n"),
    )

    assert _cli.cmd_status() == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "redis bridge (private-network ingress):" in out
    assert "Redis PING: connection refused" in out


def test_status_runner_only_has_no_gate_section(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A pure agent-runner owns no entry port — same rule as the pg/redis section."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "gate (fleet UI entry):" not in out
    assert "redis bridge (private-network ingress):" not in out


def test_status_reads_root_units(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Service rows read root units; retired watchdogs have no runtime row."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway", "agent-runner"}))
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    class _Client:
        def __init__(self, _socket_path: Path, *, timeout: float = 5.0) -> None:
            del timeout

        def status(self) -> dict[str, object]:
            return {
                "ok": True,
                "result": {
                    "units": [
                        {"id": "gateway", "state": "running", "pid": os.getpid()},
                        {"id": "frontend", "state": "running", "pid": os.getpid()},
                    ]
                },
            }

    monkeypatch.setattr("services.ava_root.client.RootClient", _Client)

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(_cli.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    out = cast(str, capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    rows = {line.split()[0]: line.split() for line in out.splitlines() if line.startswith("ava-")}
    assert rows[_sess("gateway")][1] == "✓"
    assert rows[_sess("frontend")][1] == "✓"
    assert _sess("gateway-watchdog") not in rows
    assert _sess("agent-runner-watchdog") not in rows


def test_status_root_mode_survives_an_unreachable_root(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Diagnostic-first: a down root reads as an empty tree, not a crash."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    class _Down:
        def __init__(self, _socket_path: Path, *, timeout: float = 5.0) -> None:
            del timeout

        def status(self) -> dict[str, object]:
            from services.ava_root.client import RootClientError

            raise RootClientError("unreachable in test")

    monkeypatch.setattr("services.ava_root.client.RootClient", _Down)

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(_cli.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    out = cast(str, capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    rows = {line.split()[0]: line.split() for line in out.splitlines() if line.startswith("ava-")}
    assert rows[_sess("gateway")][1] == "✗"


def test_service_row_shows_live_unit_with_skip_reason(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """A still-running session that is now gated reads as `✓ ... -- skipped: <reason>`
    — marks reflect real liveness, the suffix the gate — surfacing the mismatch the
    _print_service_row comment advertises."""
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: True)  # pyright: ignore[reportUnknownArgumentType]

    spec = next(s for s in _cli.build_services() if s.session == "browser")
    _cli._print_service_row(
        spec, 16, "no display (headless)", root_units={"browser": {"state": "running"}}
    )
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "✓" in out  # liveness mark still shown
    assert "skipped: no display" in out


# ─── prod source drift detection (any installed host) ────────────────────────


def _init_prod_source(source: Path, *, branch: str = "main") -> None:
    """Create a real git repo at `source` with one commit on `main`; optionally
    leave it checked out on a different branch (the dev-on-prod-tree mistake)."""
    import subprocess

    source.mkdir(parents=True)

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)  # noqa: S603

    run("init", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (source / "f").write_text("x")
    run("add", ".")
    run("commit", "-m", "init")
    if branch != "main":
        run("checkout", "-b", branch)


def test_detect_prod_source_drift_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No source repo → None (nothing to check)."""
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert _cli._detect_prod_source_drift() is None


def test_detect_prod_source_drift_on_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Prod source on `main` → None (no drift)."""
    _init_prod_source(tmp_path / "source")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert _cli._detect_prod_source_drift() is None


def test_detect_prod_source_drift_feature_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prod source on a feature branch → returns the branch (the 2026-06-01
    incident: an agent developing in the prod tree instead of a worktree)."""
    _init_prod_source(tmp_path / "source", branch="ava-7/fix")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert _cli._detect_prod_source_drift() == "ava-7/fix"


def test_cmd_status_warns_on_prod_source_drift(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """cmd_status surfaces the drift warning when the prod source is off main
    (runs on any installed host, here agent-runner)."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.status._detect_prod_source_drift", lambda: "ava-7/fix")
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "prod source" in out
    assert "ava-7/fix" in out


# ─── cluster pin (cluster_target_sha) status ─────────────────────────────────


def test_cluster_pin_status_no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pin set yet → None (no line to show)."""
    monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", lambda: None)
    assert _cli._cluster_pin_status() is None


def test_cluster_pin_status_returns_pin_and_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin set → (target_sha, this_host_head)."""
    monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr("cli.commands._probe._prod_source_head_sha", lambda: "abc1234")
    assert _cli._cluster_pin_status() == ("abc1234", "abc1234")


def test_cluster_pin_status_db_unreachable_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down central DB → None (ava status must still run; the pin is diagnostic)."""
    import psycopg

    def _boom() -> str | None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", _boom)
    assert _cli._cluster_pin_status() is None


def test_cmd_status_shows_cluster_pin_aligned(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """cmd_status prints the cluster-pin line; HEAD == pin → aligned."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.status._detect_prod_source_drift", lambda: None)
    monkeypatch.setattr("cli.commands.status._cluster_pin_status", lambda: ("abc1234", "abc1234"))
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "cluster pin: abc1234" in out
    assert "aligned" in out


@pytest.mark.parametrize(
    ("relation", "expected"),
    [
        ("behind", "behind pin"),
        ("ahead", "ahead of pin"),
        ("diverged", "diverged from pin"),
        ("unknown", "off pin"),
    ],
)
def test_cmd_status_cluster_pin_drift_wording(
    monkeypatch: pytest.MonkeyPatch, capsys, relation, expected
) -> None:
    """HEAD != pin: the mark reflects the git relation to the pin, not a flat 'behind'.
    'ahead' is the stray-`git pull` case the old flat wording mislabelled as behind."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.status._detect_prod_source_drift", lambda: None)
    monkeypatch.setattr("cli.commands.status._cluster_pin_status", lambda: ("aaaaaaa", "bbbbbbb"))
    monkeypatch.setattr("cli.commands.status.prod_source_pin_relation", lambda _p, _h: relation)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    assert expected in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_cmd_status_cluster_pin_head_unreadable(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """HEAD can't be read (head is None) → 'HEAD unreadable', no relation computed."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.status._detect_prod_source_drift", lambda: None)
    monkeypatch.setattr("cli.commands.status._cluster_pin_status", lambda: ("aaaaaaa", None))
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "HEAD unreadable" in out


# ─── gateway-backed CLI paths (status snapshot) ────────────────────────────
def test_cmd_status_shows_gateway_snapshot_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava status` (no flag) runs local probes AND prints the gateway snapshot."""
    _patch_gateway_http(monkeypatch)
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli.subprocess, "run", lambda *_a, **_kw: _FakeResult(returncode=0))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "httpx.get",
        lambda *_a, **_kw: _FakeResponse(  # pyright: ignore[reportUnknownArgumentType]
            {
                "machine_name": "test-host",
                "serve_gateway": True,
                "serve_agent_runner": False,
                "paused": False,
            }
        ),
    )
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert _sess("gateway") in out  # local probe section still present
    assert "gateway cluster status" in out  # gateway supplement section
    assert "test-host" in out


def test_cmd_status_gateway_unreachable_is_inline_not_fatal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A down gateway prints inline; `ava status` still returns 0 (local probes ran)."""
    import httpx

    _patch_gateway_http(monkeypatch)
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli.subprocess, "run", lambda *_a, **_kw: _FakeResult(returncode=0))  # pyright: ignore[reportUnknownArgumentType]

    def _boom(*_a, **_kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.get", _boom)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    assert "gateway unreachable" in capsys.readouterr().out


# ─── `ava status` keeps a live host reading with no observability backend ─────


def test_status_prints_a_live_host_reading(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """`ava status` reads CPU/memory/disk straight from psutil.

    Since issue #46 the host HISTORY is Prometheus's; this line is the answer
    that must survive a deployment whose LGTM backend is down or was never
    deployed, so it must not go through the observability stack at all.
    """
    from cli.commands import status as status_mod

    monkeypatch.setattr(status_mod, "_repo_root", lambda: "/repo")
    monkeypatch.setattr(status_mod, "_cluster_pin_status", lambda: ("aaaaaaa", "aaaaaaa"))
    monkeypatch.setattr(status_mod, "prod_source_pin_relation", lambda _p, _h: "aligned")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(status_mod, "_detect_prod_source_drift", lambda: None)
    monkeypatch.setattr(status_mod, "_print_gateway_cluster_status", lambda: None)
    monkeypatch.setattr(status_mod, "print_data_plane_status", lambda: None)
    monkeypatch.setattr(status_mod, "_print_service_row", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]

    assert status_mod.cmd_status() == 0
    out = cast(str, capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    assert "host (live cpu/memory/disk):" in out
    assert re.search(r"cpu \d+%\s+memory \d+% \([\d.]+/[\d.]+ GB\)\s+disk \d+%", out)


def test_status_host_reading_failure_does_not_hide_the_rest(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """A host without psutil still gets the service table and the pin section —
    the reading degrades to its own reason line, it does not abort the verb."""
    from cli.commands import status as status_mod

    monkeypatch.setattr(status_mod, "_repo_root", lambda: "/repo")
    monkeypatch.setattr(status_mod, "_cluster_pin_status", lambda: ("aaaaaaa", "aaaaaaa"))
    monkeypatch.setattr(status_mod, "prod_source_pin_relation", lambda _p, _h: "aligned")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(status_mod, "_detect_prod_source_drift", lambda: None)
    monkeypatch.setattr(status_mod, "_print_gateway_cluster_status", lambda: None)
    monkeypatch.setattr(status_mod, "print_data_plane_status", lambda: None)
    monkeypatch.setattr(status_mod, "_print_service_row", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "shared.resource_sample.resource_sample",
        lambda: (_ for _ in ()).throw(RuntimeError("no psutil here")),
    )

    assert status_mod.cmd_status() == 0
    out = cast(str, capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    assert "unavailable (no psutil here)" in out
    assert "[ava status]" in out
