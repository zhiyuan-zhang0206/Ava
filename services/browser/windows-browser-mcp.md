# browser-mcp on Windows needs a non-AF_UNIX transport

A Windows unit carries `agent-runner`
([`2026-07-28-windows-agent-runner-only.md`](../../decisions/2026-07-28-windows-agent-runner-only.md)),
and the headed browser is an agent-runner service — so unlike the gateway
blockers in [`../../gateway/windows-gateway.md`](../../gateway/windows-gateway.md), this one sits squarely
inside the supported topology.

`browser` works there. `browser-mcp` does not, and is now gated out
(`shared/platform_probes.py:browser_mcp_incapability`, consumed by
`ops/spec.py:_gate_reason`). A Windows agent-runner gets a supervised headed
Chrome reachable over CDP, and no MCP front end for it.

## What blocks it

One thing, in two places:

- `services/browser/mcp_daemon.py` — `asyncio.start_unix_server`
- `ava/_mcp_browser.py` — `asyncio.open_unix_connection` (the in-daemon line
  client that replaced the per-agent `services/browser/mcp_wrapper.py` stdio
  bridge; same AF_UNIX requirement)

Measured on the `win` runner (2026-07-29): `sys.platform win32`,
`hasattr(socket, "AF_UNIX") False`, `hasattr(asyncio, "start_unix_server")
False`, `hasattr(asyncio, "open_unix_connection") False`. Running the daemon
gives `AttributeError: module 'asyncio' has no attribute 'start_unix_server'`.

Everything above the transport is already portable: the wire format is
newline-delimited JSON (`services/browser/protocol.py`), and the daemon's two
invariants (serial lock, per-connection page affinity) are keyed on the
connection object, not on the socket family.

## The two options

**Named pipe** (`\\.\pipe\ava-browser-mcp-<cluster-slug>`) is the closer analog:
a filesystem-namespaced, machine-local, per-connection-stream endpoint with an
ACL, which is what the Unix socket is being used for. asyncio's Proactor loop
exposes `loop.start_serving_pipe` / `loop.create_pipe_connection`, so the
`StreamReader`/`StreamWriter` shape is preserved and only `_connect` / the
`start_unix_server` call change. It is Windows-only API, so the transport
becomes a two-branch module.

**Loopback TCP on a per-cluster port** is one code path for every platform and
would let the socket disappear entirely. It costs a port out of each cluster's
block and puts the browser control channel on a listening socket — which needs a
127.0.0.1 bind plus a shared-secret handshake to keep any local process off it.
The Unix socket needs no such thing (its file-permission ACL is the
authorization), so this trades a platform branch for an auth mechanism.

Named pipe is the smaller change and keeps the security property. Take TCP only
if a second consumer ever needs to reach the daemon off-box.

## What to remove when it lands

- The AF_UNIX prong in `shared/platform_probes.py:browser_mcp_incapability`
  (and `browser_mcp_incapability` itself, if the gate then equals browser's).
- `"unix_socket": true` from `ava_builtins/mcps/chrome/.mcp.json`, and — if no
  other server declares it — the `unix_socket` entry in
  `ava/_mcp_config.py:_REQUIREMENT_PROBES`.
- The POSIX-only rows in `services/agent_runner_side/browser/browser.ava.okf.md`,
  `ava_builtins/mcps/chrome/chrome.ava.okf.md`, and
  `conventions/windows-setup.md`.
