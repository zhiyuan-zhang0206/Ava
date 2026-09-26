"""Gateway status, source drift, release identity, and host readings; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import os
import re
import subprocess as subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

import cli.commands._probe as _probe_commands
import cli.commands._repo as _repo_commands
import cli.commands.status as _status_commands
import ops.roster as _roster
import shared.cluster_drift as _cluster_drift
from shared.config import settings
from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import _FakeResponse, _FakeResult, _patch_gateway_http, _sess
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base


@pytest.fixture(autouse=True)
def _local_status_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Status tests do not initialize a cluster or contact any live gateway."""
    import httpx

    from shared.root_control.client import RootClientError

    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))

    def _unreachable(*_args: object, **_kwargs: object) -> None:
        raise httpx.ConnectError("isolated status test")

    class _AbsentRoot:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def status(self) -> object:
            raise RootClientError("isolated status test")

    monkeypatch.setattr(httpx, "get", _unreachable)
    monkeypatch.setattr("shared.root_control.client.RootClient", _AbsentRoot)


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
    assert _status_commands.cmd_status() == 0
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
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _status_commands.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("gateway") in out
    assert _sess("frontend") in out


def test_status_gateway_excludes_ops(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _status_commands.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("gateway") in out
    assert _sess("ops") not in out


def test_status_agent_runner_shows_ops(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    rc = _status_commands.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("ops") in out
    assert _sess("gateway") not in out


def test_status_shows_browser_skip_reason(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Issue #1111: an enabled-but-incapable ava-browser is shown WITH its reason
    rather than silently dropped, so `ava status` (the first diagnostic command)
    is not blind to the broken service."""
    from cli.commands import _repo

    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_repo.settings.services, "browser_enabled", True)
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: "no display (headless)")
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _status_commands.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("browser") in out
    assert "skipped: no display" in out


def test_status_shows_the_gate_entry_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The entry service shares the root/identity readiness table with the app."""
    import cli.commands._probe as probe_module
    import cli.commands.status as status_module
    from cli.commands._probe import ServiceProbe

    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(status_module, "_root_tree_units", lambda: {"gate": {"state": "running"}})
    monkeypatch.setattr(
        probe_module,
        "_probe_service",
        lambda _spec: ServiceProbe(False, "down", "owned listener not ready"),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    assert _status_commands.cmd_status() == 0
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

    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        status_mod,
        "print_redis_bridge_status",
        lambda: sys.stdout.write("  ✗ 10.64.0.7:6380 Redis PING: connection refused\n"),
    )

    assert _status_commands.cmd_status() == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "redis bridge (private-network ingress):" in out
    assert "Redis PING: connection refused" in out


def test_status_runner_only_has_no_gate_section(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A pure agent-runner owns no entry port — same rule as the pg/redis section."""
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    rc = _status_commands.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "gate (fleet UI entry):" not in out
    assert "redis bridge (private-network ingress):" not in out


def test_status_reads_root_units(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Service rows read root units; retired watchdogs have no runtime row."""
    monkeypatch.setattr(
        _repo_commands, "_roles_or_none", lambda: frozenset({"gateway", "agent-runner"})
    )
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

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

    monkeypatch.setattr("shared.root_control.client.RootClient", _Client)

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _status_commands.cmd_status()
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
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    class _Down:
        def __init__(self, _socket_path: Path, *, timeout: float = 5.0) -> None:
            del timeout

        def status(self) -> dict[str, object]:
            from shared.root_control.client import RootClientError

            raise RootClientError("unreachable in test")

    monkeypatch.setattr("shared.root_control.client.RootClient", _Down)

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _status_commands.cmd_status()
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
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: True)  # pyright: ignore[reportUnknownArgumentType]

    spec = next(s for s in _roster.build_services() if s.session == "browser")
    _probe_commands._print_service_row(
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
    assert _cluster_drift.prod_source_branch_drift() is None


def test_detect_prod_source_drift_on_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Prod source on `main` → None (no drift)."""
    _init_prod_source(tmp_path / "source")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert _cluster_drift.prod_source_branch_drift() is None


def test_detect_prod_source_drift_feature_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prod source on a feature branch → returns the branch (the 2026-06-01
    incident: an agent developing in the prod tree instead of a worktree)."""
    _init_prod_source(tmp_path / "source", branch="ava-7/fix")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert _cluster_drift.prod_source_branch_drift() == "ava-7/fix"


def test_cmd_status_warns_on_prod_source_drift(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """cmd_status surfaces the drift warning when the prod source is off main
    (runs on any installed host, here agent-runner)."""
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.status._detect_prod_source_drift", lambda: "ava-7/fix")
    rc = _status_commands.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "prod source" in out
    assert "ava-7/fix" in out


# ─── release identity (replaces the retired cluster pin line) ────────────────


def _quiet_status(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Isolate `ava status` to its release section: a runner-only role, no probes."""
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.status._detect_prod_source_drift", lambda: None)
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)


def test_cmd_status_prints_no_frozen_cluster_pin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The cluster pin has no writer; a historical value must not be presented
    as the current target, nor a bare `ava cluster update` offered as a remedy."""
    _quiet_status(monkeypatch, tmp_path)
    monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", lambda **_kw: "a" * 40)

    assert _status_commands.cmd_status() == 0
    out = capsys.readouterr().out
    assert "cluster pin" not in out
    assert "ava cluster update" not in out


def test_cmd_status_names_the_selected_release_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _quiet_status(monkeypatch, tmp_path)
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases" / "current-release").write_text(
        '{"artifact_digest":"' + "a" * 64 + '","manifest_digest":"' + "b" * 64 + '"}',
        encoding="ascii",
    )

    assert _status_commands.cmd_status() == 0
    assert f"release: image {'a' * 12} (manifest {'b' * 12})" in capsys.readouterr().out


def test_cmd_status_names_the_source_checkout_it_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _quiet_status(monkeypatch, tmp_path)
    monkeypatch.setattr("shared.cluster_drift.checkout_head_sha", lambda _repo: "c" * 40)

    assert _status_commands.cmd_status() == 0
    out = capsys.readouterr().out
    assert re.search(r"release: source checkout \S+ at c{7}\n", out)


def test_cmd_status_reports_an_unreadable_selector(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An unreadable selector is shown as such, never replaced by a guess."""
    _quiet_status(monkeypatch, tmp_path)
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases" / "current-release").write_text("{}", encoding="ascii")

    assert _status_commands.cmd_status() == 0
    assert "release: ✗ selector unreadable" in capsys.readouterr().out


def test_cmd_status_shows_an_incomplete_home_operation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An interrupted release is part of the current identity: its journal phase
    and chosen direction print beside the selector."""
    from types import SimpleNamespace
    from uuid import UUID

    _quiet_status(monkeypatch, tmp_path)
    journal = tmp_path / "updates" / "op" / "operation.json"
    journal.parent.mkdir(parents=True)
    (tmp_path / "updates" / "active").write_text(f"{journal}\n")
    operation = SimpleNamespace(
        request=SimpleNamespace(kind="release", id=UUID(int=0xABCDEF)),
        phase="observing",
        direction="previous",
        error=None,
        terminal=False,
    )
    monkeypatch.setattr(
        "cli.release_transition.journal.read_operation",
        lambda path: operation if path == journal else pytest.fail(str(path)),
    )

    assert _status_commands.cmd_status() == 0
    assert (
        "  operation: release 00000000 — phase observing, direction previous"
        in capsys.readouterr().out
    )


# ─── gateway-backed CLI paths (status snapshot) ────────────────────────────
def test_cmd_status_shows_gateway_snapshot_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava status` (no flag) runs local probes AND prints the gateway snapshot."""
    _patch_gateway_http(monkeypatch)
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _FakeResult(returncode=0))  # pyright: ignore[reportUnknownArgumentType]
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
    rc = _status_commands.cmd_status()
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
    monkeypatch.setattr(_probe_commands, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _FakeResult(returncode=0))  # pyright: ignore[reportUnknownArgumentType]

    def _boom(*_a, **_kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.get", _boom)  # pyright: ignore[reportUnknownArgumentType]
    rc = _status_commands.cmd_status()
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
    monkeypatch.setattr(status_mod, "_release_identity_lines", lambda _repo: [])  # pyright: ignore[reportUnknownArgumentType]
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
    """A host without psutil still gets the service table and the release section —
    the reading degrades to its own reason line, it does not abort the verb."""
    from cli.commands import status as status_mod

    monkeypatch.setattr(status_mod, "_repo_root", lambda: "/repo")
    monkeypatch.setattr(status_mod, "_release_identity_lines", lambda _repo: [])  # pyright: ignore[reportUnknownArgumentType]
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
