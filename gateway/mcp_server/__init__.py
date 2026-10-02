"""The gateway's MCP control plane: the default-off `/mcp` endpoint and the
revocable scoped client tokens that guard it.

Package door — no imports, no re-exports; callers use the modules below:

  - `endpoint.py` — the mounted `/mcp` app (`mcp_gateway`) and its tools
  - `clients.py`  — MCP client token storage + verification
  - `router.py`   — human-credential-only `/api/mcp/clients` administration
"""
