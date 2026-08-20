# Frontend plugin contributions — declarative UI surface + blessed gateway API

Design for issue #57. Evidence base: the DeepSeek Harness plugin-ecosystem
survey (2026-08-19; 1849 npm packages / 1569 curated entries within 6 days of
their plugin platform shipping). Status: **U1 shipped** (the `contributions.ui`
manifest key + validator — declaration only); everything else here is design.
The slices at the bottom are the implementation that remains.

## Why now

plugin-spec-v2 reserved the slot ("Ava plugins have no UI surface today;
`contributions` grows a key if that ever changes") and the survey says the slot
is now warranted:

- The largest demand cluster in a real agent-plugin ecosystem is not new agent
  capabilities but *seeing and dressing the agent*: UI/themes/pets ≈19% plus
  session dashboards/usage meters ≈22% of all plugins — together outweighing
  every capability category.
- Ava already computes what those plugins surface (LGTM metrics, the cost
  ledger, the task board, fleet state). What is missing is any way for a plugin
  to put a widget, a page, or a skin in front of the user.
- The same survey shows the trap: dsh built its entire web UI as ~44
  runtime-composed client plugins over a heavyweight typed slot system, and
  deep customizers **still** routed around it — two TUIs in its top-20 chart,
  nine competing desktop shells, all alternative frontends speaking the host
  protocol directly — while shallow contributions pay a TS-build + type-chain +
  breaking-changes tax. The middle zone (runtime injection of third-party
  components into the host app) is expensive AND under-used.

## The shape: two lanes, deliberately nothing in between

| Need | Lane | Mechanism |
|---|---|---|
| Deep customization ("I want a different frontend") | **1** | An alternative frontend against the blessed gateway HTTP API |
| Shallow, high-frequency contribution (a panel, a page, a skin) | **2** | Declarative data in the plugin manifest; rendered by the host's own generic components, or a plugin-served page in a sandboxed iframe |
| Anything between (third-party components composed into the app at runtime) | — | **Rejected** — see the rejected list |

`ui/web` itself stays a single-team, coherent Next.js application with no
internal slot system. That is what keeps it cheap to refactor — and the token
theme design below is what keeps refactors from breaking contributions.

## Lane 1 — alternative frontends against the gateway API

The dsh evidence is that deep customizers build against the host protocol no
matter what the host offers. Fighting that is waste; blessing it is cheap,
because the contract already exists:

- `scripts/dump_openapi.py` dumps the FastAPI app's OpenAPI spec and is already
  the single source of truth for the frontend's own generated types
  (`openapi-typescript` codegen, drift-gated by the `types-codegen-fresh`
  pre-commit hook). `ui/web` is, mechanically, already an API client with no
  private side channel.
- Auth is the cluster's normal surface: bearer = cluster secret, or the cookie
  session (auth-tls-design phases 1–2, deployed). An alternative frontend
  authenticates exactly like `ui/web` does.

What "blessed" adds (implementation slice, not new machinery):

1. **Publish the spec as an artifact** — `openapi.json` is already checked in
   at `ui/web/openapi.json`; serve it from the gateway (`GET /api/openapi.json`
   is FastAPI-native) and say in the docs that this is the third-party frontend
   contract.
2. **A stability statement**, in `conventions/`: additive changes are free;
   removing or renaming a path/field gets a CHANGELOG note. No versioned API,
   no deprecation windows — single-digit consumers, source-visible spec, the
   codegen diff makes every contract change reviewable in the PR that makes it.
3. **Scope**: the contract is `/api/*` (including the SSE streams — they are
   how `ui/web` gets live data and any real frontend needs them). Explicitly
   NOT contract: `/ops` (operator internals), Next.js routes, and the
   frontend's own component structure.

## Lane 2 — declarative contributions

Core invariant: **the frontend never executes third-party JS as part of its own
composition.** A plugin's UI contribution is data — declarations rendered by
generic host components — plus, at most, a page the plugin serves itself,
embedded in an iframe. Worst case is a broken page inside one iframe, never a
broken app.

### The manifest key — `contributions.ui`

plugin-spec-v2's reserved growth point, filled:

```json
{
  "contributions": {
    "ui": {
      "agentInspect": [
        { "title": "Memory pool", "source": "api/inspect", "render": "kv" }
      ],
      "nav": [
        { "location": "sidebar", "label": "Task board", "icon": "kanban", "page": "board/" }
      ],
      "themes": [
        { "name": "solarized", "tokens": { "--background": "oklch(0.99 0.02 90)", "...": "..." } }
      ]
    }
  }
}
```

Validated by `shared/plugin_manifest.py` like every other manifest field:
unknown contribution type = validator error (closed set, fail fast), unknown
theme token = validator error (the vocabulary is the `globals.css` custom
property set), `icon` from a closed icon-name set (lucide names — data, not
markup). Like `skills`/`commands`, UI contributions are pure declarations
consumed directly from the manifest — there is no runtime `register_*` call, so
the S3 declared-vs-registered diff does not apply to this key.

### Aggregation — `GET /api/ui/contributions`

The gateway reads the enabled plugins' manifests and returns the merged
declaration set (name-attributed, so the frontend can label provenance and an
operator can trace a surface back to its plugin). The frontend fetches it like
any server data (TanStack Query). No per-plugin frontend code, no build step.

### Plugin-served pages — the universal escape hatch

Custom visuals come from a page the plugin serves itself, reached through the
gateway exactly like `ava.ui` pages are today (`gateway/routers/pages.py`
reverse-proxies `/api/pages/<agent_id>-<name>/…` to a supervised local page
server, behind the gateway's normal auth). Plugin pages get the sibling mount:

- `/api/plugin-ui/<plugin>/<path>` → reverse-proxy to the plugin's declared
  page backend: either a **converge-synced static dir** (preferred — converge
  already materializes plugin images; a static page needs no process) or the
  plugin's **ops service** (`contributions.opsServices` — the existing surface;
  needed only for dynamic pages).
- Same path-segment validation, same forwarded-header allowlist, same
  404-when-disabled semantics as `pages.py`.
- The frontend embeds it in a **sandboxed iframe**. The sandbox is **breakage
  containment, not a security boundary**: plugin Python already runs in agent
  processes with shell and DB access, so the trust decision is made at install
  time (the install scan gate + trust tier,
  `future/infra/skill-supply-chain-trust.md`) — a plugin page learns nothing
  its Python half does not already have. What the iframe buys is that a
  broken/slow page cannot take down the console, and the app's own bundle
  stays free of third-party code.

`nav` entries open such a page from the sidebar, the settings area, or the
fleet toolbar. This is where everything unforeseen lands (task boards, balance
panels, pet widgets) without growing the declaration vocabulary.

### Contribution types (v1, closed set)

| Type | Declaration | Frontend behavior |
|---|---|---|
| `agentInspect` section | `{title, source, render: markdown\|kv\|table\|page}` | appends a section to the agent-inspect view (`gateway/routers/agent_inspect.py` feeds it); `source` is a path under the plugin's mount, fetched through the proxy and rendered by a generic markdown/kv/table renderer; `render: page` embeds the iframe with `?agent_id=` |
| `nav` entry | `{location: sidebar\|settings\|fleet-toolbar, label, icon, page}` | a nav entry opening the plugin's page in an iframe |
| `theme` | `{name, tokens}` | registers a skin — see below |
| metrics / config | *no declaration* | already-automatic surfaces (below) |

### Themes — token packs, never CSS

A theme is a named set of values for the existing `globals.css` oklch custom
properties (`--background`, `--primary`, `--chart-*`, …) — the same token layer
next-themes' light/dark switch already targets. The settings area grows a theme
picker; the choice persists as a `user_settings` `display.*` key via
`useUserSettings` (the frontend state policy in `ui/web/AGENTS.md`: durable
preferences are DB rows, never localStorage), and the frontend applies the pack
by setting the custom properties on the root element.

Tokens only — no arbitrary CSS, no selectors. That is what makes skins survive
UI refactors (the token vocabulary is the stable interface; component markup is
not) and what makes a theme incapable of injecting behavior or layout breakage.
A token pack may be partial (unset tokens keep the default), and each token
value is validated as a CSS color literal.

### Already automatic — completed by this design, not new machinery

- **Metrics**: `shared/plugin_metrics.py` already defines the two-tier
  core/plugin metric architecture with an `inspector` output surface reserved
  for per-agent panels under `/api/agents/{id}/inspect`
  (`get_agent_plugin_metrics` exists). Finishing that reserved surface is part
  of this design's scope: a plugin metric declared for `inspector` renders as
  an inspect panel with zero UI declaration.
- **Config**: plugin config schemas are Pydantic models in
  `shared/plugin_config_registry.py`; the settings area auto-renders them as
  forms. A plugin gets a settings page by having config at all.

## Slotting into plugin-spec-v2

- `contributions.ui` amends the spec's "Not borrowed from VS Code" item 2: Ava
  now borrows VS Code's *themes/views* contribution **shape** (declarative,
  host-rendered) while still not borrowing runtime component composition
  (webviews are the closest VS Code analog to our iframes — also a deliberate
  process/DOM boundary, not in-app composition).
- **Ordering**: the manifest key + validator is S0–S2-grade work (declaration
  only, zero runtime behavior — same red line as the shipped validator slices)
  and can land any time. The aggregation endpoint, the proxy mount, and the
  renderers are runtime work and land with or after S3, without depending on
  the S3 lifecycle machinery itself.
- The `machine`/`enabled_set` context dimensions come from issue #39 (below).

## Composition with in-flight work (do not contradict)

- **#39 (extension ownership: cluster content / machine capability / agent
  activation)**: UI contributions are naturally **cluster-scoped** — the
  frontend is per-cluster, so `GET /api/ui/contributions` reads the cluster's
  enabled set (registry rows once #39-S4 lands; `plugins_config.json` until
  then). Per-agent activation (#39-S3) filters the *agent-inspect* sections
  only: a section for a plugin not active on the inspected agent is omitted
  from that agent's inspect view. Nav and themes are cluster-level surfaces
  and ignore per-agent overlays.
- **#41 (`ava plugins inspect`)**: declared `ui` contributions are listed like
  any other surface — no special casing; since UI contributions have no
  registration side, they appear in the "manifest declarations" half only.
- **#42 (four-layer modification model)**: UI contributions ride
  `ava-plugin.json` through the normal L3 install flow, so an external plugin
  adds a panel or skin without a kernel PR — exactly the decoupling #42's L3
  ladder requires. Builtin plugins declare theirs in-tree like any manifest
  field.

## Rejected (recorded to stop re-litigation)

- **Runtime component injection / an in-app typed slot system** (the dsh middle
  zone). Survey citations: ~44 runtime-composed client plugins to build dsh's
  own UI; deep customizers bypassed it anyway (two TUIs in the top 20, nine
  competing desktop shells against the host protocol); shallow contributors pay
  a TS-build + type-chain + breaking-changes tax. It is the price of
  "everything is a plugin" — an ideology Ava explicitly does not hold
  (plugin-spec-v2 borrow/not-borrow). Deep customization is Lane 1.
- **Build-time React component contributions** (plugins ship TSX, converge
  rebuilds the frontend): couples every plugin install to a Node toolchain on
  every gateway host, a broken plugin breaks the whole app build, violates
  fail-fast isolation. Rejected.
- **Composer / conversation-surface extension points** — dsh's hottest UI
  cluster (file drop, @file, prompt toolboxes) targets a chat-first product;
  Ava's interaction center of gravity is IM bridge / CLI / notifications.
  Revisit only on real demand.
- **User-customizable memory/compaction semantics** (ruling 2026-08-19):
  semantic mechanisms belong to the kernel; the survey's "memory plugins" are
  pre-existing products retrofitting platform support, not user demand.
  User-level memory workflows are already expressible as skills + the memory
  pool.
- **Arbitrary-CSS skins**: tokens only (see themes).
- **Do nothing** (frontend stays closed): leaves the single largest
  demonstrated plugin-demand cluster unservable and pushes trivial needs (a
  usage widget, a skin) into full frontend forks. Rejected by ruling
  2026-08-19 — skins are explicitly wanted as part of this surface.

## Implementation remaining (slices, each independently landable)

- **U1 — manifest key + validator** — **shipped**: the `contributions.ui`
  schema lives in `shared/plugin_ui_contributions.py` (closed type set, closed
  icon vocabulary, and a theme token vocabulary locked against
  `ui/web/src/app/globals.css` by
  `tests/shared/test_plugin_ui_contributions.py`, so a token the console adds
  fails the suite until it is offered to skins or listed as non-themable).
  `shared/plugin_manifest.py` calls it for the `ui` key. Declaration-only;
  zero runtime change. `--radius` is deliberately non-themable — a theme pack
  is colors, and re-valuing the radius is a layout change in a theme's
  clothes.
- **U2 — themes end-to-end**: aggregation endpoint (themes subset) + settings
  theme picker + `user_settings` persistence + root-element token application.
  The smallest slice that exercises the whole declaration→aggregation→render
  chain, with no proxy or iframe involved.
- **U3 — plugin page mount + nav**: `/api/plugin-ui/<plugin>/…` reverse-proxy
  (static-dir and ops-service backends) + sandboxed-iframe page component +
  nav entries from the aggregation endpoint.
- **U4 — agent-inspect sections**: generic markdown/kv/table renderers over
  proxied `source` endpoints + the `page` variant; plus finishing the reserved
  `inspector` surface of `shared/plugin_metrics.py`.
- **U5 — bless the API**: serve `openapi.json` from the gateway + the
  `conventions/` contract page with the stability statement (Lane 1).

U2–U4 order is preference, not dependency; all consume U1's declarations.
