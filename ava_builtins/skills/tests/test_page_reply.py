"""Page resources submit user input through the existing authenticated origin."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGES = Path(__file__).parents[3] / "ava_builtins/skills/platform/ava-guide/pages"


def _reply_body(resource: str) -> str:
    if resource == "reply":
        return (PAGES / "widgets/ava_reply/reply.js").read_text()
    html = (PAGES / f"widgets/{resource}/{resource}.html").read_text()
    start = html.index("  const AVA_AGENT_ID =")
    end = html.index("\n  // --- render", start)
    return html[start:end]


@pytest.mark.parametrize("resource", ["reply", "choice", "confirm", "form", "compare"])
@pytest.mark.parametrize("status", [201, 401])
def test_reply_uses_current_origin_and_reports_delivery_failure(resource: str, status: int) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the shipped browser resource")
    body = _reply_body(resource).replace("__AGENT_ID__", "42").replace("__PAGE_NAME__", "pick")
    harness = f"""
let request;
globalThis.fetch = async (url, options) => {{
  request = {{url, ...options}};
  return {{ok: {json.dumps(status < 400)}, status: {status}, json: async () => ({{id: 7}})}};
}};
{body}
let result;
let error;
try {{ result = await avaReply('choice: B'); }} catch (e) {{ error = e.message; }}
process.stdout.write(JSON.stringify({{request, result, error}}));
"""
    run = subprocess.run(  # noqa: S603 — only shipped JavaScript and fixed fixture values
        [node, "--input-type=module", "-e", harness], capture_output=True, text=True, check=True
    )
    output = json.loads(run.stdout)
    request = output["request"]
    assert request["url"] == "/api/agents/42/messages"
    assert request["credentials"] == "same-origin"
    assert request["method"] == "POST"
    assert request["headers"] == {"Content-Type": "application/json"}
    assert json.loads(request["body"]) == {"content": "choice: B", "source": "ui:page:pick"}
    if status < 400:
        assert output["result"] == {"id": 7}
        assert "error" not in output
    else:
        assert output["error"] == "avaReply failed: HTTP 401"
        assert "result" not in output
