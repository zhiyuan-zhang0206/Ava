"""Self-takeover bootstrap names the launching agent, validates before launch, and pins the shared app-server topology."""

import importlib.util
import json
import shutil
import subprocess
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
    # The request command spells out its former defaults (task #4102: the CLI
    # defaults are gone).
    assert "--ttl 3600" in message and "--batch-window 0" in message
    assert str(guide) in message and brief in message
    assert "work.md" not in message and "tasks.md" not in message and "work file" not in message
    assert "ava impersonate say" in message
    assert "ava.impersonation.say" not in message
    assert "release with your own summary" in message
    assert "transport acceptance is not host receipt" in message
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
    assert "delivers into that same server" in message
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
    assert "AP=${!}" in server and "kill $AP" in server
    assert "if ! kill -0 $$ 2>/dev/null; then" in server
    assert "ps -p $AP -o command= 2>/dev/null | grep -q 'app-server'" in server
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


def test_app_server_wait_accepts_a_bound_socket() -> None:
    import shutil
    import socket as socket_module
    import tempfile
    import threading

    module = _load_spawn("takeover_spawn_codex_wait_ok")
    # A bound AF_UNIX path must stay under the kernel's ~104-byte limit, and
    # macOS pytest tmp dirs (/private/var/folders/...) exceed it (review C2) —
    # build a short private dir under the system temp root instead.
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
        module._wait_for_app_server(f"unix://{path}", timeout=5.0)
    finally:
        # Join before closing the listener: under CI load the accept thread can
        # be scheduled only after the waiter returns; closing first makes the
        # pending accept() raise EBADF on a dead fd and the observation this
        # test asserts is lost (backend shard 6/16 red, 2026-09-17).
        thread.join(timeout=5)
        listener.close()
        shutil.rmtree(short_dir, ignore_errors=True)
    assert accepted == [True]


def test_app_server_wait_fails_loudly_when_absent(tmp_path: Path) -> None:
    module = _load_spawn("takeover_spawn_codex_wait_missing")
    log_path = tmp_path / "app-server.log"
    with pytest.raises(RuntimeError, match="did not become ready") as excinfo:
        module._wait_for_app_server(
            f"unix://{tmp_path}/missing.sock", timeout=0.4, log_path=log_path
        )
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
    """Feed the real launcher line through the production pane shape.

    The pane is a pty running ``bash -l -i`` (PtySessionBackend), whose history
    expansion can abort a whole line containing ``!`` (review C1). Whether
    ``$!`` itself is expanded is a bash-version fact — 3.2 (macOS) aborts the
    whole line, 5.x (this host, CI) exempts it — so this is a discriminating
    guard on biting shells and an execution-level smoke everywhere: the line
    must reach the server command and leave the pane usable. A login shell
    rebuilds PATH (macOS path_helper prepends /etc/paths), so the shim is
    exported inside the pane rather than inherited (macmini red, 2026-09-17).
    The other tests here monkeypatch ``sessions.send`` and assert the command's
    shape only;
    this one covers the real execution face (task #3778).
    """
    import contextlib
    import os
    import signal
    import time

    module = _load_spawn("takeover_spawn_codex_pane_exec")
    state = tmp_path / "home"
    state.mkdir()
    owner = _owner(state)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    endpoint = f"unix://{run}/codex-app-server.0123456789ab-01234567.sock"
    command = module._app_server_command(owner, workspace, endpoint)

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
        # A login pane rebuilds PATH once /etc/profile runs — macOS path_helper
        # prepends /etc/paths (homebrew) and masks an inherited shim, pointing
        # the launch at the real codex (macmini red, 2026-09-17). Export the
        # shim first, inside the pane, so the launcher line runs the shim.
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
        # Job control gives the shim its own process group; target it by its
        # unique tmp path (no other process carries it), and the janitor
        # self-exits once the pane shell is gone.
        subprocess.run(["pkill", "-f", str(tmp_path)], check=False)  # noqa: S603
        os.close(master)

    text = output.decode(errors="replace")
    assert "event not found" not in text, text[-2000:]
    assert argv_file.exists(), text[-2000:]
    assert f"app-server --listen {endpoint}" in argv_file.read_text()
    assert "SENTINEL_4" in text, text[-2000:]


@pytest.mark.parametrize("provider", ["codex", "claude", "dsh"])
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


def test_claude_bootstrap_resident_routing_names_the_plugin() -> None:
    guide = _REFERENCE.parents[3] / ".agents/skills/impersonator-guide/SKILL.md"
    message = bootstrap_message(42, "Fix login", "claude", "brief", guide, relay_resident=True)
    assert "Ava relay plugin" in message
    assert "do not arm a Monitor watch" in message
    assert "fall back to the manual flow" in message
    assert "Immediately start the Claude Monitor relay" not in message


def test_codex_bootstrap_ignores_the_resident_flag() -> None:
    guide = _REFERENCE.parents[3] / ".agents/skills/impersonator-guide/SKILL.md"
    message = bootstrap_message(42, "Fix login", "codex", "brief", guide, relay_resident=True)
    assert "CODEX_THREAD_ID" in message and "CODEX_HOME" in message
    assert "Ava relay plugin" not in message


def test_dsh_bootstrap_names_the_session_plugin_relay() -> None:
    guide = _REFERENCE.parents[3] / ".agents/skills/impersonator-guide/SKILL.md"
    message = bootstrap_message(42, "Fix login", "dsh", "brief", guide)
    assert "--provider dsh" in message and "--as 'DeepSeek Harness: Fix login'" in message
    assert "Ava relay plugin loaded into this DeepSeek Harness session" in message
    assert "Monitor" not in message and "--codex-remote" not in message


def test_dsh_launcher_boots_headless_with_the_relay_plugin_as_its_runner(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("takeover_spawn_dsh", _REFERENCE / "spawn_dsh.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = _REFERENCE / "ava-relay-dsh" / "ava-relay.mjs"
    assert plugin == module._PLUGIN and plugin.is_file()
    patch = module._patch(plugin, tmp_path / "launch.txt")
    assert "- id: headless-runner\n  disabled: true" in patch
    assert "- id: headless-startup\n  disabled: true" in patch
    assert f"name: {json.dumps(str(plugin))}" in patch
    assert f"takeoverFile: {json.dumps(str(tmp_path / 'launch.txt'))}" in patch
    command = module._dsh_command(tmp_path, "/bin/node", "/bin/dsh", tmp_path / "p.yml")
    assert command == (
        f"cd {tmp_path} && DSH_PERMISSION_MODE=danger-full-access "
        f"exec /bin/node /bin/dsh --profile headless --patch {tmp_path / 'p.yml'}"
    )


def test_dsh_relay_emits_one_json_string_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    """The plugin splits relay stdout by line: a multi-line envelope must stay one line."""
    from cli.commands.impersonation_relay import host_emitter

    host_emitter("dsh", None)("Ava message agent=42\n[id=7] kind=chat\nhi")
    assert capsys.readouterr().out == '"Ava message agent=42\\n[id=7] kind=chat\\nhi"\n'


_PLUGIN_HARNESS = """
import { existsSync, writeFileSync } from 'node:fs'
const [pluginPath, relayPy, out, mode, takeoverFile] = process.argv.slice(2)
const plugin = await import(pluginPath)
const steered = [], followed = [], created = [], disposers = []
let contributor, stub
const agent = {
  id: 'session-1', session: { header: { id: 'session-1' } },
  steer: (m) => steered.push(m), followup: (m) => followed.push(m),
}
const services = {
  loader: { await: async () => {} },
  agentDefaultModel: { currentSelection: () => ({ provider: 'p', model: 'm' }) },
}
const finish = (extra) => {
  writeFileSync(out, JSON.stringify({ steered, followed, created, ...extra }))
  disposers.forEach((dispose) => dispose())
}
const ctx = {
  agents: {
    get: (id) => (id === 'session-1' ? agent : undefined),
    create: async (options) => { created.push(options); return { agent } },
  },
  shellEnv: { register: (c) => { contributor = c } },
  effect: (fn) => { disposers.push(fn()) },
  on: () => {},
  get: (name) => services[name],
  jobs: {
    start(spec) {
      const hooks = spec.run()
      hooks.done.then((outcome) => finish({
        kind: spec.kind, owned: spec.owner === agent, outcome,
        stubGone: !existsSync(stub), anonymous: contributor.resolve({}),
      }))
      return 'ava-relay-1'
    },
  },
}
plugin.apply(ctx, mode === 'takeover' ? { takeoverFile } : {})
if (mode === 'takeover') {
  setTimeout(() => finish({ launchGone: !existsSync(takeoverFile) }), 200)
} else {
  stub = contributor.resolve({ agent }).DSH_AVA_RELAY_STUB
  writeFileSync(stub, `SID=3\\nAGENT=42\\nAVA_IMPERSONATION_RELAY_TOKEN=tok-3\\nAVA_IMPERSONATION_RELAY_PY=${relayPy}\\n`)
}
"""


def _run_plugin(tmp_path: Path, *args: str) -> dict[str, object]:
    harness = tmp_path / "harness.mjs"
    harness.write_text(_PLUGIN_HARNESS, encoding="utf-8")
    out = tmp_path / "out.json"
    plugin = _REFERENCE / "ava-relay-dsh" / "ava-relay.mjs"
    subprocess.run(  # noqa: S603 — fixed argv: node, the test harness and fixture paths
        ["node", str(harness), plugin.as_uri(), args[0], str(out), *args[1:]],
        check=True,
        timeout=60,
    )
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("node") is None, reason="the dsh plugin runs on Node.js")
def test_dsh_plugin_relays_the_session_stub_into_that_session(tmp_path: Path) -> None:
    """Stub -> relay child with the scoped token -> each JSON line steered, envelopes intact."""
    envelope = "Ava message agent=42 lease=3 ids=7\n[id=7] kind=chat from=user\nhi"
    texts = tmp_path / "texts.json"
    texts.write_text(json.dumps(["Ava control active.", envelope]), encoding="utf-8")
    relay = tmp_path / "relay.sh"
    relay.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {tmp_path}/argv.txt\n'
        f'printf "%s" "$AVA_IMPERSONATION_RELAY_TOKEN" > {tmp_path}/token.txt\n'
        f"exec {sys.executable} -c 'import json, sys; "
        "from cli.commands.impersonation_relay import plugin_dsh; "
        f"[plugin_dsh(text) for text in json.load(open(sys.argv[1]))]' {texts}\n",
        encoding="utf-8",
    )
    relay.chmod(0o755)
    result = _run_plugin(tmp_path, str(relay), "relay")
    assert (tmp_path / "argv.txt").read_text().split() == [
        "-m", "cli", "impersonate", "relay", "42", "--session", "3", "--provider", "dsh",
    ]  # fmt: skip
    assert (tmp_path / "token.txt").read_text() == "tok-3"
    steered = result["steered"]
    assert isinstance(steered, list)
    assert [m["content"][0]["text"] for m in steered] == ["Ava control active.", envelope]
    assert all(m["role"] == "user" for m in steered)
    assert all(
        m["source"] == {"kind": "plugin", "plugin": "ava-relay", "form": "relay"} for m in steered
    )
    assert result["kind"] == "ava-relay" and result["owned"] and result["stubGone"]
    assert result["outcome"] == {"status": "completed", "detail": "exit code: 0"}
    assert result["anonymous"] == {}


@pytest.mark.skipif(shutil.which("node") is None, reason="the dsh plugin runs on Node.js")
def test_dsh_plugin_takeover_runner_submits_and_consumes_the_launch_message(
    tmp_path: Path,
) -> None:
    launch = tmp_path / "launch.txt"
    launch.write_text("You will take over Ava agent 42.", encoding="utf-8")
    result = _run_plugin(tmp_path, "unused", "takeover", str(launch))
    created = result["created"]
    followed = result["followed"]
    assert isinstance(created, list) and isinstance(followed, list)
    assert [c["agentOptions"] for c in created] == [{"provider": "p", "model": "m"}]
    assert [m["content"][0]["text"] for m in followed] == ["You will take over Ava agent 42."]
    assert followed[0]["source"] == {"kind": "user"}
    assert result["launchGone"]
