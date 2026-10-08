"""Shared helpers and fixtures for the test_commands_* split files; split from tests/components/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import subprocess as _subprocess
from pathlib import Path

import pytest

# Explicit shared surface: every name the split test modules import from here.
__all__ = [
    "_FakeResponse",
    "_FakeResult",
    "_FakeSessionBackend",
    "_fake_session_backends",
    "_gateway_role_pinned",
    "_git_aware",
    "_hermetic_gateway_base",
    "_patch_gateway_http",
    "_sess",
    "_spec",
]


def _sess(service: str) -> str:
    """Expected composed session name (`ava-<service>` — no cluster segment;
    the per-home session backend scopes sessions)."""
    return f"ava-{service}"


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# Captured before any monkeypatch: the start-path tests stub subprocess.run to
# intercept session / docker / probe commands, but `ava start`'s migration step
# consults git (`base.deploy.schema.migrations._tracked_migration_paths`, Task #998) — a
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

    Stands in for the native service supervisor.
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

    `ava start` / `ava stop` drive the native supervisor for service sessions;
    they must not reach the
    real supervisor in unit tests (a real launch would fork a daemon, a real
    kill could touch the dev host's sessions). Returns (service, shell).
    """
    import base.sessions.backend as _sb

    service = _FakeSessionBackend()
    shell = _FakeSessionBackend()
    monkeypatch.setattr(_sb, "get_backend", lambda: service)
    monkeypatch.setattr(_sb, "get_shell_backend", lambda: shell)
    return service, shell


@pytest.fixture(autouse=True)
def _gateway_role_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the machine's capability set to gateway, whatever the dev host's environment says.

    The cluster-status commands read it through `_roles_or_none`; the tests assert the
    full-service gateway view, so the host's own `AVA_MACHINE_SERVE_*` flags must not decide."""
    monkeypatch.setattr("base.cluster.machine.machine_role", lambda: frozenset({"gateway"}))


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
    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gw:8000")


def _spec(service: str):
    from cli.commands._repo import ServiceSpec
    from ops.roster.service_spec import (
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
    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gw:8000")


def _assert_named_commands_parse(text: str) -> None:
    """Every backticked `ava ...` command in an operator hint parses; none is a
    bare `ava cluster update`, which requires a prepared release request."""
    import re

    from cli.parsers import build_parser

    parser = build_parser()
    for command in re.findall(r"`(ava [^`]+)`", text):
        parser.parse_args(command.split()[1:])  # SystemExit(2) fails the test
    assert "ava cluster update" not in text
