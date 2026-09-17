"""Self-takeover bootstrap names the launching agent, validates before launch, and pins the shared app-server topology."""

import importlib.util
import sys
from pathlib import Path

import pytest

from ava._impersonation_launch import bootstrap_message
from shared import coding_session_owner

_REFERENCE = (
    Path(__file__).parents[2] / "ava_builtins/skills/ava-use-claude-code-and-codex/reference"
)
_ENDPOINT = "unix:///home/u/.ava-lc/run/codex-app-server.0123456789ab-01234567.sock"


def _load_spawn(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, _REFERENCE / "spawn_codex.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _owner(state_dir: Path) -> coding_session_owner.CodingSessionOwner:
    key = coding_session_owner.CodingSessionKey(
        cluster="/cluster", workspace="/workspace", tool="codex"
    )
    return coding_session_owner.CodingSessionOwner(
        key=key, status="active", owner_agent_id=1, state_dir=state_dir
    )


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_self_takeover_bootstrap_inlines_brief_and_links_real_guide(
    provider: str, tmp_path: Path
) -> None:
    guide = _REFERENCE.parents[3] / ".agents/skills/impersonator-guide/SKILL.md"
    assert guide.is_file()
    brief = "Goal: fix the login flow.\nDecision: keep the session table as-is."
    message = bootstrap_message(42, "Fix login", provider, brief, guide)
    assert "take over Ava agent 42" in message
    assert "--agent 42" in message and "--name 'Fix login'" in message
    assert str(guide) in message and brief in message
    assert "work.md" not in message and "tasks.md" not in message and "work file" not in message
    assert "ava impersonate say" in message
    assert "ava.impersonation.say" not in message
    assert "release with your own summary" in message
    assert "queue acceptance is not host receipt" in message
    if provider == "codex":
        assert "CODEX_THREAD_ID" in message and "CODEX_HOME" in message
        assert "--codex-remote" in message
    else:
        assert "Monitor relay with --session" in message
        assert "as the request output instructs" in message
        assert "--codex-remote" not in message


def test_codex_bootstrap_carries_the_shared_app_server_endpoint() -> None:
    guide = _REFERENCE.parents[3] / ".agents/skills/impersonator-guide/SKILL.md"
    message = bootstrap_message(42, "Fix login", "codex", "brief", guide, codex_remote=_ENDPOINT)
    assert f"--codex-remote {_ENDPOINT}" in message
    assert "queues into that same server" in message
    assert "CODEX_THREAD_ID" in message and "CODEX_HOME" in message


def test_takeover_launcher_wires_one_explicit_shared_app_server(tmp_path: Path) -> None:
    module = _load_spawn("takeover_spawn_codex_shared_server")
    state = tmp_path / "home"
    state.mkdir()
    owner = _owner(state)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    endpoint = f"unix://{tmp_path}/run/codex-app-server.0123456789ab-01234567.sock"
    server = module._app_server_command(owner, workspace, endpoint)
    tui = module._codex_command(owner, workspace, None, remote=endpoint)
    assert f"codex app-server --listen {endpoint}" in server
    assert "AP=$!" in server and "kill $AP" in server
    assert f"rm -f {endpoint.removeprefix('unix://')}" in server
    assert 'approval_policy="never"' in server
    assert 'sandbox_mode="danger-full-access"' in server
    assert f"--remote {endpoint} --dangerously-bypass-approvals-and-sandbox" in tui
    assert tui.startswith("clear && ")


def test_supervised_launch_command_is_unchanged(tmp_path: Path) -> None:
    module = _load_spawn("takeover_spawn_codex_supervised")
    state = tmp_path / "home"
    state.mkdir()
    owner = _owner(state)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    command = module._codex_command(owner, workspace)
    assert command == (
        f"cd {workspace} && CODEX_HOME={state} "
        "exec codex --dangerously-bypass-approvals-and-sandbox"
    )


def test_app_server_wait_accepts_a_bound_socket(tmp_path: Path) -> None:
    import socket as socket_module
    import threading

    module = _load_spawn("takeover_spawn_codex_wait_ok")
    path = tmp_path / "probe.sock"
    listener = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    accepted: list[bool] = []

    def accept_once() -> None:
        connection, _ = listener.accept()
        accepted.append(True)
        connection.close()

    thread = threading.Thread(target=accept_once, daemon=True)
    thread.start()
    try:
        module._wait_for_app_server(f"unix://{path}", timeout=5.0)
    finally:
        listener.close()
        thread.join(timeout=5)
    assert accepted == [True]


def test_app_server_wait_fails_loudly_when_absent(tmp_path: Path) -> None:
    module = _load_spawn("takeover_spawn_codex_wait_missing")
    with pytest.raises(RuntimeError, match="did not become ready"):
        module._wait_for_app_server(f"unix://{tmp_path}/missing.sock", timeout=0.4)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_launch_requires_native_identity_before_creating_workspace(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location(
        f"takeover_spawn_{provider}", _REFERENCE / f"spawn_{provider}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "must-not-be-created"
    monkeypatch.setattr(sys, "argv", [f"spawn_{provider}.py", str(target), "--impersonate-self"])

    def no_identity() -> None:
        raise RuntimeError("No launching Ava identity")

    monkeypatch.setattr("ava._boot.require_agent_id", no_identity)
    with pytest.raises(RuntimeError, match="No launching Ava identity"):
        module.main()
    assert not target.exists()
