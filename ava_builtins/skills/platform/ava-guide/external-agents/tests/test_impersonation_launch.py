"""Launcher and impersonator-guide contracts for external coding agents."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ava.shell.coding_tools import codex
from base.sessions import coding_session_owner
from base.sessions.coding_session_owner_record import CodingSessionStatus

_REPO = Path(__file__).parents[6]
_SKILL_DIR = Path(__file__).parents[1]


def _owner(state_dir: Path) -> coding_session_owner.CodingSessionOwner:
    key = coding_session_owner.CodingSessionKey(
        cluster="/cluster", workspace="/workspace", tool="codex"
    )
    return coding_session_owner.CodingSessionOwner(
        key=key, status=CodingSessionStatus.ACTIVE, owner_agent_id=1, state_dir=state_dir
    )


def test_impersonator_guide_carries_receipt_semantics_and_the_borrowed_context() -> None:
    """Task #5010: an ACK is receipt and the takeover inherits the borrowed identity's
    standing context — both must be present in the guide."""
    guide = _REPO / "ava_builtins/skills/platform/impersonator-guide/SKILL.md"
    text = guide.read_text(encoding="utf-8")
    assert "## Before acting: inherit the borrowed context" in text
    assert "memory/MEMORY.md" in text
    assert "ava.tasks" in text
    assert "substitute new-agent assumptions" in text
    assert "Read [message receipt](reference/message-receipt.md)" in text
    assert "before handling a delivered batch" in text
    assert "ACK never means completion" in text
    receipt = (guide.parent / "reference/message-receipt.md").read_text(encoding="utf-8")
    _assert_message_receipt_contract(text, receipt)


def _assert_message_receipt_contract(entry: str, receipt: str) -> None:
    assert "A truncated push is not received yet" in receipt
    assert "ava agents timeline <agent_id>" in receipt
    assert "ava.context.gateway.get" in receipt
    assert "receipt, not completion" in receipt
    package_text = entry + receipt
    assert "process what arrives, then acknowledge it" not in package_text
    assert "Acknowledge only what you actually handled" not in package_text
    assert "ACK the cancel once stopped" not in package_text


def test_codex_launcher_wires_one_explicit_shared_app_server(tmp_path: Path) -> None:
    state = tmp_path / "home"
    state.mkdir()
    owner = _owner(state)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    endpoint = f"unix://{tmp_path}/run/codex-app-server.0123456789ab-01234567.sock"
    server = codex.app_server_command(owner, workspace, endpoint)
    tui = codex.codex_command(workspace, None, remote=endpoint)
    assert f"codex app-server --listen {endpoint}" in server
    assert "AP=${!}" in server and "kill $AP" in server
    assert "if ! kill -0 $$ 2>/dev/null; then" in server
    assert "ps -p $AP -o command= 2>/dev/null | grep -q 'app-server'" in server
    assert f"rm -f {endpoint.removeprefix('unix://')}" in server
    assert 'approval_policy="never"' in server
    assert 'sandbox_mode="danger-full-access"' in server
    assert "CODEX_HOME" not in server and "check_for_update_on_startup=false" in server
    assert f"--remote {endpoint} --dangerously-bypass-approvals-and-sandbox" in tui
    assert tui.startswith("clear && ")


def test_app_server_wait_accepts_a_bound_socket() -> None:
    import shutil
    import socket as socket_module
    import tempfile
    import threading

    short_dir = Path(tempfile.mkdtemp(prefix="ava-f1-", dir=tempfile.gettempdir()))
    path = short_dir / "probe.sock"
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
        codex.wait_for_app_server(f"unix://{path}", timeout=5.0)
    finally:
        thread.join(timeout=5)
        listener.close()
        shutil.rmtree(short_dir, ignore_errors=True)
    assert accepted == [True]


def test_app_server_wait_fails_loudly_when_absent(tmp_path: Path) -> None:
    log_path = tmp_path / "app-server.log"
    with pytest.raises(RuntimeError, match="did not become ready") as excinfo:
        codex.wait_for_app_server(f"unix://{tmp_path}/missing.sock", timeout=0.4, log_path=log_path)
    assert str(log_path) in str(excinfo.value)


def _pump_pane(master: int, seconds: float, buf: bytearray) -> None:
    import os
    import select
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([master], [], [], 0.05)
        if ready:
            try:
                buf.extend(os.read(master, 65536))
            except OSError:
                return


def _write_codex_shim(shim_dir: Path, argv_file: Path) -> None:
    shim_dir.mkdir()
    shim = shim_dir / "codex"
    shim.write_text(f'#!/bin/sh\necho "$@" > {argv_file}\necho FAKE_CODEX_STARTED\nsleep 60\n')
    shim.chmod(0o755)


def _start_pane(env: dict[str, str], cwd: Path) -> tuple[int, subprocess.Popen[bytes]]:
    import os
    import pty

    master, slave = pty.openpty()
    pane = subprocess.Popen(
        ["bash", "-l", "-i"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    os.close(slave)
    return master, pane


def test_app_server_command_executes_in_an_interactive_bash(tmp_path: Path) -> None:
    import contextlib
    import os
    import signal
    import time

    state = tmp_path / "home"
    state.mkdir()
    owner = _owner(state)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    endpoint = f"unix://{run}/codex-app-server.0123456789ab-01234567.sock"
    command = codex.app_server_command(owner, workspace, endpoint)

    shim_dir = tmp_path / "bin"
    argv_file = tmp_path / "codex-argv.txt"
    _write_codex_shim(shim_dir, argv_file)

    env = dict(os.environ)
    env["PATH"] = f"{shim_dir}:{env['PATH']}"
    env["HOME"] = str(tmp_path / "fakehome")
    env["PS1"] = "P> "
    (tmp_path / "fakehome").mkdir()

    master, pane = _start_pane(env, workspace)
    output = bytearray()
    try:
        _pump_pane(master, 1.0, output)
        os.write(master, f'export PATH="{shim_dir}:$PATH"\n'.encode())
        _pump_pane(master, 0.5, output)
        os.write(master, (command + "\n").encode())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not argv_file.exists():
            _pump_pane(master, 0.5, output)
        _pump_pane(master, 1.0, output)
        os.write(master, b"echo SENTINEL_$((2+2))\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and b"SENTINEL_4" not in output:
            _pump_pane(master, 0.5, output)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pane.pid, signal.SIGKILL)
        subprocess.run(["pkill", "-f", str(tmp_path)], check=False)  # noqa: S603
        os.close(master)

    text = output.decode(errors="replace")
    assert "event not found" not in text, text[-2000:]
    assert argv_file.exists(), text[-2000:]
    assert f"app-server --listen {endpoint}" in argv_file.read_text()
    assert "SENTINEL_4" in text, text[-2000:]


@pytest.mark.parametrize("provider", ["codex", "claude", "dsh", "antigravity"])
def test_launch_rejects_missing_workspace_without_creating_it(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location(
        f"takeover_spawn_{provider}", _SKILL_DIR / "scripts" / f"spawn_{provider}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    target = tmp_path / "must-not-be-created"
    monkeypatch.setattr(sys, "argv", [f"spawn_{provider}.py", str(target), "--prompt", "hi"])
    with pytest.raises(SystemExit):
        module.main()
    assert not target.exists()


def test_dsh_launcher_boots_headless_with_the_relay_plugin_as_its_runner(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "takeover_spawn_dsh", _SKILL_DIR / "scripts" / "spawn_dsh.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = _SKILL_DIR / "scripts" / "ava-relay-dsh" / "ava-relay.mjs"
    assert plugin == module._PLUGIN and plugin.is_file()
    patch = module._patch(plugin, tmp_path / "launch.txt")
    assert "- id: headless-runner\n  disabled: true" in patch
    assert "- id: headless-startup\n  disabled: true" in patch
    assert f"name: {json.dumps(str(plugin))}" in patch
    assert f"takeoverFile: {json.dumps(str(tmp_path / 'launch.txt'))}" in patch
    command = module._dsh_command(tmp_path, "/bin/node", "/bin/dsh", tmp_path / "p.yml")
    assert command == (
        f"cd {tmp_path} && DSH_PERMISSION_MODE=danger-full-access "
        f"/bin/node /bin/dsh --profile headless --patch {tmp_path / 'p.yml'}; "
        "printf 'dsh %s with status %s\\n' exited \"$?\""
    )
    assert not module._EXITED.search(command)  # the echoed command never reads as an exit


def test_impersonator_sdk_reference_covers_instruction_read_and_direct_python() -> None:
    guide = _REPO / "ava_builtins/skills/platform/impersonator-guide/SKILL.md"
    text = guide.read_text(encoding="utf-8")
    assert "scripts/read_instructions.py" in text
    assert "reference/sdk.md" in text
    sdk_text = (guide.parent / "reference/sdk.md").read_text(encoding="utf-8")
    assert "scripts/read_instructions.py 0 --agent 405" in sdk_text
    assert "Use direct Python as the normal SDK path" in sdk_text


def test_impersonator_guide_routes_replies_by_request_origin() -> None:
    guide = _REPO / "ava_builtins/skills/platform/impersonator-guide/SKILL.md"
    text = guide.read_text()
    assert "Read [message receipt](reference/message-receipt.md)" in text
    assert "reply at the request's entry point" in text
    text = (guide.parent / "reference/message-receipt.md").read_text()

    assert "Codex opened manually in tmux) gets plain text there" in text
    assert "`ava impersonate say` in Ava" in text
    assert "Ava message agent=... lease=... ids=..." in text
    assert "item `from=` source" in text
    assert "host's user role alone" in text
    assert "Keep the reply route with each request" in text
    assert "Relay ACK is separate" in text
    assert "Host text does not replace the required Ava release summary" in text
