"""The extension surfaces: installed skills, the cross-machine plugin + MCP
enable matrix, plugin-served UI pages, console contributions, and the
skill/plugin/MCP install entry.

Package door — no imports, no re-exports; each router module is mounted in
`gateway/app.py`:

  - `skills.py`           — `GET|PUT /api/skills`
  - `inventory.py`        — `GET|PUT /api/inventory`
  - `plugin_ui.py`        — `GET /api/plugin-ui/{plugin}/{path}`
  - `ui_contributions.py` — `GET /api/ui/contributions`
  - `packages.py`         — `POST /api/packages/draft`
  - `schemas.py`          — the surfaces' wire models
"""
