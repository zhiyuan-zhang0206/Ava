"""Wire models of the extension surfaces: the cross-machine plugin + MCP
enable matrix (`/api/inventory`), the installed-skills panel (`/api/skills`),
and the console contribution aggregate (`/api/ui/contributions`).

Skills are a single-host, gateway-local read: this host's `$AVA_HOME/skills/`
load dir correlated with the install registry (`installed.json`). Skills are
per-machine (the registry is machine-local; there is no cluster-shared row), so
unlike the cross-machine plugin/MCP inventory this is not a `?machine=` matrix.

The contribution aggregate is the wire form of what the cluster's enabled
plugins declare under `contributions.ui` (`base/packages/plugins/ui_contributions.py`).
Every entry is name-attributed: the console labels provenance, and an operator
tracing a surface back to the plugin that put it there reads one field. Themes,
nav entries, and statistics-panel cards today — the slice that carries
agent-inspect sections adds its own array beside these (additive, so an
alternative frontend built against today's spec keeps working).

FastAPI registers these unchanged, so the OpenAPI codegen is byte-identical to
the wire before.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class InventoryItemHostState(BaseModel):
    """One plugin/MCP server's state on ONE host, in the cross-machine aggregate.

    `present` is False when the host doesn't have this plugin/server at all
    (installed elsewhere but not here) — the cell renders "not installed" and
    offers no toggle. When present, `enabled` is the effective state and
    can_enable/reason are the host's read-time capability verdict (None = no gate).
    """

    present: bool
    enabled: bool
    can_enable: bool | None = None
    reason: str | None = None


class InventoryItemAggregate(BaseModel):
    """One plugin or MCP server, collapsed across every reachable host.

    `kind` is "plugin" | "mcp". `hosts` maps each reachable machine name to its
    per-host state (a machine lacking this item -> present=False).
    """

    name: str
    kind: str  # "plugin" | "mcp"
    description: str
    hosts: dict[str, InventoryItemHostState]


class InventoryAggregate(BaseModel):
    """GET /api/inventory response — the cross-machine plugin + MCP matrix.

    `machines` is every machine name considered (the column set). `unreachable`
    is the subset whose inventory read timed out / failed (their cells are
    unknown). `plugins` / `mcp_servers` are the collapsed rows.
    """

    machines: list[str]
    unreachable: list[str]
    plugins: list[InventoryItemAggregate]
    mcp_servers: list[InventoryItemAggregate]


class InventoryItem(BaseModel):
    """One plugin/MCP server on a SINGLE host (the ?machine= view)."""

    name: str
    kind: str  # "plugin" | "mcp"
    enabled: bool
    can_enable: bool | None = None
    reason: str | None = None
    description: str


class InventoryMachineView(BaseModel):
    """GET /api/inventory?machine= response — one host's plugins + MCP servers."""

    machine: str
    plugins: list[InventoryItem]
    mcp_servers: list[InventoryItem]


class InventoryItemWriteResult(BaseModel):
    """Per-item verdict of a PUT /api/inventory write (`ok` + reason when rejected)."""

    ok: bool
    reason: str | None = None


class InventoryWriteResult(BaseModel):
    """PUT /api/inventory response — per-item results + atomic `applied` flag."""

    applied: bool
    plugin_results: dict[str, InventoryItemWriteResult]
    mcp_results: dict[str, InventoryItemWriteResult]


SkillLayer = Literal["core", "plugin", "machine", "untracked"]
"""Where a skill's content comes from — the registry `origin`, surfaced:

- "core" — a repo skill (`<repo>/ava_builtins/skills/<name>/`), converge-managed.
- "plugin" — bundled by a plugin (`plugins/<p>/skills/`), converge-managed.
- "machine" — user-installed / hand-registered on this machine (`ava skill
  register` / `ava plugins install <git-url>`); converge never touches it.
- "untracked" — a directory in the load dir with no registry entry: present on
  disk but the skill scanner will not load it.
"""


class SkillView(BaseModel):
    """One skill in the load dir, correlated with its registry entry.

    `modified_locally` is the converge "was hand-edited" signal — the on-disk
    copy's tree hash no longer matches the `content_hash` converge last wrote
    (always False for machine/untracked entries, which carry no managed hash).
    `origin_path` is the source tree a converge-managed copy derives from.
    """

    name: str
    layer: SkillLayer
    enabled: bool
    modified_locally: bool
    origin_path: str | None = None


class SkillsView(BaseModel):
    """Every top-level directory in this host's skills load dir."""

    skills: list[SkillView]


class SkillEnableUpdate(BaseModel):
    """Request body for PUT /api/skills — toggle one skill's enabled flag."""

    name: str
    enabled: bool


class UiThemeContribution(BaseModel):
    """One named token pack a plugin offers the theme picker.

    `tokens` is a partial map of the console's own `:root` custom properties to
    color literals — validated at manifest load, so the console applies the
    values as given. Unset tokens keep the console default, which is why a pack
    that names three colors is a legitimate skin rather than a broken one.

    `dark_tokens` is the pack's dark-mode half, and `null` is a MEANINGFUL
    value rather than a missing one: it declares that the pack deliberately
    pins both modes to `tokens`. That distinction has to survive onto the wire
    because it is what the picker tells the user — a pack applied over both
    palettes silently disables the light/dark toggle for every color it sets,
    and a skin and a mode that fight is the failure this field exists to make
    visible. With it set, `tokens` is the light half and the two stay
    orthogonal.
    """

    plugin: str
    name: str
    tokens: dict[str, str]
    dark_tokens: dict[str, str] | None = None


class UiNavContribution(BaseModel):
    """One nav entry opening a plugin-served page.

    `page` is a path under the plugin's own mount (`/api/plugin-ui/<plugin>/`),
    `icon` a lucide icon name from the closed set the console imports, and
    `location` names which of the console's nav surfaces carries the entry —
    all three validated at manifest load.
    """

    plugin: str
    location: str
    label: str
    icon: str
    page: str


class UiStatContribution(BaseModel):
    """One statistics-panel card a plugin declares.

    Declaration only: the card's existence and label. Its value is runtime
    data keyed by `(plugin, id)` in `plugin_stats` (written by the plugin's own
    refresh code, read through `GET /api/stats/dashboard`), and the panel joins
    the two halves on `(plugin, id)`. A declared card with no value row is an
    explicit empty state — a card can exist on a machine whose credentials do
    not, which is the case this split exists to serve.
    """

    plugin: str
    id: str
    label: str


class UiContributionsResponse(BaseModel):
    """Every console contribution the cluster's enabled plugins declare."""

    themes: list[UiThemeContribution]
    nav: list[UiNavContribution]
    stats: list[UiStatContribution]
