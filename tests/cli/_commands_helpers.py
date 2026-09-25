"""Shared helpers and fixtures for the test_commands_* split files; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import subprocess as _subprocess
from pathlib import Path

import pytest

from cli import commands as _cli

# Explicit shared surface: every name the split test modules import from here.
__all__ = [
    "_FakeResponse",
    "_FakeResult",
    "_FakeSessionBackend",
    "_fake_session_backends",
    "_git_aware",
    "_hermetic_gateway_base",
    "_noop_start_prechecks",
    "_patch_gateway_http",
    "_real_register_machine_or_die",
    "_sess",
    "_spec",
]


def _sess(service: str) -> str:
    """Expected composed session name (`ava-<service>` — no cluster segment;
    the per-home session backend scopes sessions)."""
    return f"ava-{service}"


# _noop_start_prechecks (autouse) monkey-patches _register_machine_or_die on the
# _cli module. Keep a reference to the real implementation so tests can exercise
# its actual behaviour.
_real_register_machine_or_die = _cli._register_machine_or_die


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# Captured before any monkeypatch: the start-path tests stub subprocess.run to
# intercept session / docker / probe commands, but `ava start`'s migration step
# consults git (`shared.migrations._tracked_migration_paths`, Task #998) — a
# blank fake result would trip the git-tracking gate's fail-closed path and
# abort cmd_start. `git` invocations therefore reach the real binary (read-only
# rev-parse / ls-files, milliseconds).
_REAL_SUBPROCESS_RUN = _subprocess.run


def _git_aware(fake):
    """Wrap a fake subprocess.run so `git ...` calls still hit real git."""

    def _run(args, **kwargs):
        if args and args[0] == "git":
            return _REAL_SUBPROCESS_RUN(args, **kwargs)  # pyright: ignore[reportUnknownArgumentType]
        return fake(args, **kwargs)

    return _run


class _FakeSessionBackend:
    """In-memory session backend: records new/kill, answers has_session from a set.

    Stands in for the service backend (native supervisor on POSIX, winproc
    on Windows).
    """

    def __init__(self) -> None:
        self.alive: set[str] = set()
        self.created: list[str] = []
        self.killed: list[tuple[str, bool]] = []
        self.new_ok = True
        self.graceful_result: tuple[bool, str] = (True, "graceful")
        self.force_result: tuple[bool, str] = (True, "forced")
        self.signalled: list[str] = []

    def has_session(self, name: str) -> bool:
        return name in self.alive

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> bool:
        self.created.append(name)
        if self.new_ok:
            self.alive.add(name)
        return self.new_ok

    def kill_session(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str]:
        self.killed.append((name, graceful))
        if graceful:
            ok, mode = self.graceful_result
        else:
            ok, mode = self.force_result
        if ok:
            self.alive.discard(name)
        return ok, mode

    def graceful_signal(self, name: str) -> bool:
        self.signalled.append(name)
        return name in self.alive

    def list_sessions(self, prefix: str = "") -> list[str]:
        return sorted(n for n in self.alive if n.startswith(prefix))


@pytest.fixture(autouse=True)
def _fake_session_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeSessionBackend, _FakeSessionBackend]:
    """The session backends, faked in-memory for every test in an importing module.

    `ava start` / `ava stop` drive the service backend (native supervisor on
    POSIX, winproc on Windows) for service sessions; it must not reach the
    real supervisor in unit tests (a real launch would fork a daemon, a real
    kill could touch the dev host's sessions). Returns (service, shell).
    """
    import shared.session_backend as _sb

    service = _FakeSessionBackend()
    shell = _FakeSessionBackend()
    monkeypatch.setattr(_sb, "get_backend", lambda: service)
    monkeypatch.setattr(_sb, "get_shell_backend", lambda: shell)
    return service, shell


@pytest.fixture(autouse=True)
def _noop_start_prechecks(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_start's multi-machine setup collection + converge_host + register_self
    are all noop in an importing module — here we test session / docker / stop / status call shapes,
    orthogonal to setup. Setup behavior itself is left to shared/test_machine.py + the setup-ergonomics tests in `test_commands_start.py`.

    Default role="gateway" (full service set). To test secondary, explicitly override:
        monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
        monkeypatch.setattr(_cli, "_collect_setup_values", lambda _a: (..., []))"""

    def _fake_collect(_args: dict[str, str | None]) -> tuple[dict[str, str], list]:
        return {
            "machine_name": "test-machine",
            "machine_role": "gateway",
            "memory_remote": "git@github.com:test/AvaMemory.git",
            "gateway_url": "http://test-gateway:8000",
        }, []

    monkeypatch.setattr(_cli, "_collect_setup_values", _fake_collect)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "admit_live_start", lambda *_a, **_kw: False)
    monkeypatch.setattr(_cli, "converge_host", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    # The per-cluster pg/redis bring-up (`_ensure_gateway_data_plane`) starts a real
    # native instance under $AVA_HOME. These tests assert session/stop/status call
    # shapes, not infra, so stub it to a noop — keeping them hermetic regardless of
    # the dev host's pg/redis.
    from cli.commands import start as _start_mod

    monkeypatch.setattr(_start_mod, "_ensure_gateway_data_plane", lambda: 0)
    monkeypatch.setattr("cli.commands._data_plane.prepare_gateway_schema", lambda: None)
    monkeypatch.setattr("cli.commands._data_plane.complete_gateway_data_plane", lambda **_kw: None)
    from cli.commands._root_driver import LaunchOutcome

    monkeypatch.setattr(
        _cli, "_launch_service_tree", lambda roster, *_a, **_kw: LaunchOutcome(roster, ())
    )
    monkeypatch.setattr(
        _cli,
        "_wait_for_service_tree",
        lambda *_a, **_kw: _cli.ReadinessWait((), 0.0, sessions_gone=False),
    )

    # _roles_or_none (stop/status/converge) + machine_role (cmd_start service
    # resolution) both read settings + the machine_serve_* files; test env has
    # no file → empty/Missing. Pin both to gateway so the default path is the
    # full-service gateway box, deterministic regardless of the dev host's
    # machine_serve_* files. Agent-runner tests override machine_role explicitly.
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    # register_self goes to central DB UPSERT; test does not need real writes. cmd_start goes
    # through _register_machine_or_die which internally imports register_self, directly patch the helper to return 0.
    monkeypatch.setattr(_cli, "_register_machine_or_die", lambda _resolved, _role: 0)  # pyright: ignore[reportUnknownArgumentType]
    # secondary path will run _probe_gateway_or_die; primary does not call it, adding here
    # ensures secondary tests can also reuse the default noop.
    monkeypatch.setattr(_cli, "_probe_gateway_or_die", lambda _url: 0)  # pyright: ignore[reportUnknownArgumentType]
    # _assert_schema_current_or_die truly calls DB; tests don't need real schema query, directly patch.
    monkeypatch.setattr(_cli, "_assert_schema_current_or_die", lambda: 0)
    # Root service preparation must not install frontend dependencies in unit tests.
    from cli.commands import _repo, _root_driver

    monkeypatch.setattr(_repo, "_ensure_frontend_deps", lambda _repo: None)  # pyright: ignore[reportUnknownArgumentType]

    # These call-shape tests use the suite's owner DB URL, not an enrolled
    # runner's bootstrap projection. Credential forwarding has its own tests
    # in test_agent_profile_launch_env.py.
    def _fixture_runner_url(_url: str) -> str:
        return "postgresql://ava_runner:test-runner@127.0.0.1:1/ava_citest"

    monkeypatch.setattr(_root_driver, "runner_db_url_projection", _fixture_runner_url)


@pytest.fixture(autouse=True)
def _hermetic_gateway_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `ava status` / `ava cluster status` HTTP calls off any real gateway.

    `cmd_status`'s gateway supplement and `cmd_cluster_status`'s roster both dial
    `gateway_api_base()`; left unstubbed they hit whatever gateway the dev box
    happens to be running (the live prod one), so the test outcome would depend
    on the environment — a live gateway on older code even crashes the supplement
    on a renamed field. Resolve it to an unreachable stub by default: tests that
    assert on the response mock httpx on top; the rest take the graceful
    'unreachable' path deterministically, matching CI where no gateway is up."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")


def _spec(service: str):
    from cli.commands._repo import ServiceSpec
    from ops.service_spec import (
        _GATEWAY,  # typed frozenset[MachineRole]; capability irrelevant to probe tests
    )

    return ServiceSpec(
        session=service,
        cmd="x",
        capabilities=_GATEWAY,
        requires_db=True,  # irrelevant to these probe tests
        curl_url="http://localhost:1/",
    )


class _FakeResponse:
    def __init__(self, payload: dict | list, status_code: int = 200):
        self._payload = payload  # pyright: ignore[reportUnknownMemberType]
        self.status_code = status_code

    def raise_for_status(self) -> None:
        import httpx

        if self.status_code >= 400:
            request = httpx.Request("GET", "http://gw:8000")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(f"{self.status_code}", request=request, response=response)

    def json(self) -> dict | list:
        return self._payload  # pyright: ignore[reportUnknownMemberType]


def _patch_gateway_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub gateway URL/headers resolution so the HTTP helpers don't hit settings."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
