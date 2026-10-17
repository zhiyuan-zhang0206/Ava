import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SKILL_DIR = (
    Path(__file__).parents[4] / "meiva_builtins/skills/platform/meiva-guide/external-agents"
)


def test_dsh_relay_emits_one_json_string_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    from cli.commands.agents.impersonation_adapters import resolve_adapter

    resolve_adapter("dsh", None).send("Meiva message agent=42\n[id=7] kind=chat\nhi")
    assert capsys.readouterr().out == '"Meiva message agent=42\\n[id=7] kind=chat\\nhi"\n'


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
      return 'meiva-relay-1'
    },
  },
}
plugin.apply(ctx, mode === 'takeover' ? { takeoverFile } : {})
if (mode === 'takeover') {
  setTimeout(() => finish({ launchGone: !existsSync(takeoverFile) }), 200)
} else {
  stub = contributor.resolve({ agent }).DSH_MEIVA_RELAY_STUB
  writeFileSync(stub, `SID=3\\nAGENT=42\\nMEIVA_IMPERSONATION_RELAY_TOKEN=tok-3\\nMEIVA_IMPERSONATION_RELAY_PY=${relayPy}\\n`)
}
"""


def _run_plugin(tmp_path: Path, *args: str) -> dict[str, Any]:
    harness = tmp_path / "harness.mjs"
    harness.write_text(_PLUGIN_HARNESS, encoding="utf-8")
    out = tmp_path / "out.json"
    plugin = _SKILL_DIR / "scripts" / "meiva-relay-dsh" / "meiva-relay.mjs"
    subprocess.run(
        ["node", str(harness), plugin.as_uri(), args[0], str(out), *args[1:]],
        check=True,
        timeout=60,
    )
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("node") is None, reason="the dsh plugin runs on Node.js")
def test_dsh_plugin_relays_the_session_stub_into_that_session(tmp_path: Path) -> None:
    envelope = "Meiva message agent=42 lease=3 ids=7\n[id=7] kind=chat from=user\nhi"
    texts = tmp_path / "texts.json"
    texts.write_text(json.dumps(["Meiva control active.", envelope]), encoding="utf-8")
    relay = tmp_path / "relay.sh"
    relay.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {tmp_path}/argv.txt\n'
        f'printf "%s" "$MEIVA_IMPERSONATION_RELAY_TOKEN" > {tmp_path}/token.txt\n'
        f"exec {sys.executable} -c 'import json, sys; "
        "from cli.commands.agents.impersonation_adapters import emit_dsh; "
        f"[emit_dsh(text) for text in json.load(open(sys.argv[1]))]' {texts}\n",
        encoding="utf-8",
    )
    relay.chmod(0o755)
    result = _run_plugin(tmp_path, str(relay), "relay")
    assert (tmp_path / "argv.txt").read_text().split() == [
        "-m",
        "cli",
        "impersonate",
        "relay",
        "42",
        "--session",
        "3",
        "--provider",
        "dsh",
    ]
    assert (tmp_path / "token.txt").read_text() == "tok-3"
    steered: list[dict[str, Any]] = result["steered"]
    assert [m["content"][0]["text"] for m in steered] == ["Meiva control active.", envelope]
    assert all(m["role"] == "user" for m in steered)
    assert [m["source"] for m in steered] == [
        {"kind": "plugin", "plugin": "meiva-relay", "form": "notice", "summary": summary}
        for summary in ("Meiva control active.", "Meiva message agent=42 lease=3 ids=7")
    ]
    assert result["kind"] == "meiva-relay" and result["owned"] and result["stubGone"]
    assert result["outcome"] == {"status": "completed", "detail": "exit code: 0"}
    assert result["anonymous"] == {}


@pytest.mark.skipif(shutil.which("node") is None, reason="the dsh plugin runs on Node.js")
def test_dsh_plugin_takeover_runner_submits_and_consumes_the_launch_message(
    tmp_path: Path,
) -> None:
    launch = tmp_path / "launch.txt"
    launch.write_text("You will take over Meiva agent 42.", encoding="utf-8")
    result = _run_plugin(tmp_path, "unused", "takeover", str(launch))
    created: list[dict[str, Any]] = result["created"]
    followed: list[dict[str, Any]] = result["followed"]
    assert [c["agentOptions"] for c in created] == [{"provider": "p", "model": "m"}]
    assert [m["content"][0]["text"] for m in followed] == ["You will take over Meiva agent 42."]
    assert followed[0]["source"] == {"kind": "user"}
    assert result["launchGone"]
