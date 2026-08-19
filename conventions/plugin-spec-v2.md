# Plugin Spec v2 — package identity, dependencies, lifecycle

The contract layer for every installable extension package: one manifest, one
set of validation rules, one lifecycle vocabulary. What exists today is
**S0–S2** — the manifest contract, the validator, and install-time enforcement
only. Nothing in this spec changes runtime behavior today; the lifecycle and
context sections (S3–S5) are the normative contract for work that lands after
the open-source release, synchronized with the MCP runtime redesign
(`cli-mcp-forward` design, task #1212). Design rationale and evidence:
`decisions/` entries cited below and the spec-v2 draft (task #1243).

## Why this exists

Three incidents, one missing layer each:

- **#1198** — an MCP package's `pyproject.toml` pinned `mcp>=1.27.0` with no
  upper bound; `uv sync` pulled 2.0.0 and the old API crashed. The install path
  had no mechanism to stop the next package from making the same mistake.
- **#1047 / #974** — the install registry had no version/dependency model, so
  "what is installed" and "what it claims to be" could drift with no signal.
- **2026-08-12 17:57** — a test process resolved the real `~/.ava/.env`, wrote
  synthetic agents into the prod DB. Extensions had no way to declare what
  context they require, and dangerous context was reachable by default.

Plus the gaps these expose: packages have no identity/version contract, no
dependency declarations, no dispose contract, no ready/failed state, and no
context declaration.

## Goals

1. **Every package has an identity** — name + version + host compatibility
   range + content hash.
2. **Dependencies are declared, resolvable, refusable** — plugin dependencies,
   Python package ranges, and host capabilities; checked at install time,
   refused when missing or out of range.
3. **One lifecycle state machine** — `installed → enabled → ready → failed →
   disposed`, visible and auditable (S3).
4. **Context is first-class** — every instance binds an explicit context;
   dangerous context (prod DB writes) is unreachable unless declared (S5).
5. **No runtime rewrite** — the seven plugin injection surfaces, the install
   registry, converge, and the CLI surface all stay as they are. This spec is
   a contract layer, not a new runtime.

## The core decision: one manifest, layered runtimes

**Unified metadata (one manifest) + unified lifecycle vocabulary (one state
machine), layered runtimes (three payload kinds keep their existing executors).**
No Cordis-style "everything is a plugin" runtime unification — see the
borrow/not-borrow section.

- A Claude Code plugin package is already a mixed release unit (skills +
  commands + `.mcp.json`); `ava_builtins/plugins/ava_code` ships skills in the
  same repo. One manifest declares all contribution surfaces; the host
  dispatches each to its existing executor (the VS Code model).
- Unifying runtimes is a misfit: skills are pure text with no runtime, MCP is a
  cross-process standard protocol (redesign owned by #1212). The Cordis values
  — dependency declaration, dispose discipline, isolation — are taken without
  the service graph.
- The seven injection surfaces are mature and incident-hardened (namespace
  fail-fast, reducer-aware hook merge, wrap review contract, schema-drift
  auto-merge). Rewriting them as a service graph would re-buy those lessons.
- Config overlay is **not** a package form. It is instance parameterization
  (per-machine / per-agent knobs); the manifest's `config` contribution
  declares the schema those knobs write (PR-E / per-agent overlay remains
  future work).
- The three runtimes share one vocabulary and one registry row; tooling sees
  one flat surface.

## The manifest — `ava-plugin.json`

Placed at the package root. The name is **`ava-plugin.json`** (user ruling
2026-08-13): the Claude Code `.claude-plugin/plugin.json` has no
version/dependency/lifecycle fields and its semantics stay ecosystem-owned;
the adapter (`cli/commands/_claude_code_plugin.py`) translates it. A package
without a manifest keeps working through the legacy detection path — the
manifest is opt-in, and for now nothing ships one.

Example (the shape `ava_code` would declare):

```json
{
  "apiVersion": 2,
  "name": "ava-code",
  "version": "1.3.0",
  "description": "coding conventions + AGENTS.md auto-injection",
  "engines": { "ava": ">=0.1.38" },
  "contributions": {
    "hooks": ["after_init", "before_llm"],
    "sdkNamespaces": ["cwd"],
    "sdkWraps": ["files.read"],
    "systemPromptSections": ["context_file"],
    "opsServices": ["task_maintenance"],
    "config": { "schema": "default_config.py", "perAgentFields": ["marker"] },
    "skills": ["skills/"],
    "commands": ["commands/"],
    "mcpServers": ["."]
  },
  "dependencies": {
    "plugins": {},
    "pythonPackages": [],
    "hostCapabilities": {}
  },
  "lifecycle": {
    "entry": "plugin.py",
    "activation": "immediate",
    "dispose": "effect-registry"
  }
}
```

### Field semantics

| Field | Meaning | Closes |
|---|---|---|
| `name` | Unique identity; dash/underscore-folded to the directory name (registry already folds via `shared.skill_names.match_key`) | duplicate rows / name collisions |
| `version` (semver) | Package version; recorded beside `installed_hash` as `installed_version` when the registry schema v2 fields land (S3) | no version awareness (A1) |
| `engines.ava` (semver range) | Host compatibility interval (npm `engines` / VS Code `engines.vscode` shape); checked at install/upgrade | framework evolution silently breaking plugins (A3) |
| `dependencies.plugins` | Plugin-to-plugin dependencies `{"other": ">=1.0"}`; resolved before load, missing → `failed`, never silently skipped (resolution lands S3) | load order by luck (A8) |
| `dependencies.pythonPackages` | The Python dependency ranges this package is known to work with. For MCP packages this is the **mirror / validation anchor** of `pyproject.toml` dependencies. **Hard enforcement** (user ruling 2026-08-13): a declared range without an upper bound is a validator error, and an install/upgrade whose pyproject range falls outside the declared range is refused | **#1198, permanently**: unbounded or drifting pyproject ranges are stopped at install time |
| `dependencies.hostCapabilities` | Host capability declarations — `db: none|ro|rw`, `network: none|local|any`, `shell: none|any`, `display: none|required`, `unixSocket: none|required`. The execution side (capability gates on resource injection) lands with the context model (S5); today the manifest only declares, and MCP runtime keeps its existing `requires` check | context/capability declarations unified (D12/D13); generalizes the MCP `requires` keys |
| `contributions.*` | Declared contribution surfaces (VS Code `contributes` analog). **The declaration is documentation; registration is fact.** The diff between the two is already computed and readable — `ava plugins inspect <name>` reports it (`agent/plugin_catalog.py:declared_vs_registered`, over the attribution ledger every `register_*` writes); S3 turns that same computation into the load-time gate (declared-but-not-registered = warning, registered-but-not-declared = fail-fast) | surfaces pre-checkable, listable, auditable |
| `config.schema` / `config.perAgentFields` | Pointer to the config schema (the Pydantic model, or a declarative schema) + which fields per-agent overlays may override | PR-E; pre-install config validation without importing plugin code |
| `lifecycle.*` | See the lifecycle section | dispose contract (C9/C10) |

### Version ranges

A semver range is a conjunction (AND) of clauses, comma- or space-separated:
`>=1,<2`. Operators: `>=`, `>`, `<=`, `<`, `==`, `=`; a bare version means
`==`. No OR, no wildcards, no prerelease ordering — prerelease suffixes are
accepted on versions but not ordered. **An upper bound (`<`/`<=`/`==`) is a
hard validator requirement for `dependencies.pythonPackages` entries** — the
#1198 lesson: unbounded = eventually pulled past the break.

## Implemented today (S0–S2)

The validator lives in `shared/plugin_manifest.py` (parse + validate +
range algebra + the pyproject mirror check). Enforcement points:

| Surface | What runs | When |
|---|---|---|
| `ava plugins install`, `ava skill install`, `ava mcp install` | manifest pre-validation: structure, ranges, `engines.ava` vs the host version | before landing; missing manifest → legacy path, unchanged |
| `ava mcp install`, `ava mcp upgrade` | `dependencies.pythonPackages` mirror check against the fetched `pyproject.toml` | before landing; unbounded pyproject or a range outside the declared one → refused |
| any manifest consumer | upper-bound lint: an unbounded `pythonPackages` range is a **validator error**, not a warning | at validation |

Deliberately **not** implemented today (red line: zero runtime behavior
change): no load-time contribution diff, no state transitions, no registry
schema change, no context gates. Existing packages — none of which ship a
manifest — behave exactly as before.

## Lifecycle contract (S3+, normative)

One state machine for all three payload kinds, two axes (per-machine,
per-agent):

```
installed ─enable─▶ enabled ─(activate)─▶ ready ─▶ (running, failure)──▶ failed
   ▲                   │    ▲                                     │
   └── uninstall ◀─────┘    └──── disable ◀───────────────────────┘
```

- `installed` — landed + registry row (today's semantics).
- `enabled` — the per-machine switch (today: `plugins_config.json` /
  `mcp_enabled.json` / registry `enabled`; S3 reads all three as one state
  bit, keeps all three write paths).
- `ready` — **new**. plugin = loaded + `after_init` passed; MCP = initialize +
  `requires` met + tool list fetched (this is the ready signal #1212's gateway
  routing table consumes); skill = always ready (no runtime).
- `failed` — **new**. Activation failure records `last_error` + timestamp —
  today an enabled plugin that silently disappears is the hardest bug to
  diagnose. **Recovery semantics (user ruling 2026-08-13): `failed` →
  dispose; the next load attempt retries; manual re-enable is the explicit
  path back. No automatic resurrection loop.**
- Disable/update: dispose the old instance, then load the new one; a failed
  dispose is recorded and does not block.

### Reload boundary — no in-process hot reload (user ruling 2026-08-13 13:18)

**The reload boundary is the agent process's `self.restart`.** There is no
in-process hot-reload requirement; a process restart is the natural backstop
that guarantees a clean state. `dispose` therefore means *ordered cleanup at
restart / uninstall / disable* — never a mid-flight in-process swap of a live
plugin. Future implementers must not design for in-process reload; if
something looks like it needs one, the answer is a restart.

### Dispose contract (S4)

Resource registration replaces "the author remembers to clean up":

- `ctx.effect(cleanup_fn)` — register a cleanup closure; disposed LIFO, each
  exception recorded, none blocking the rest.
- Every existing `register_*` call (hook/state/config/namespace/wrap/service/
  metric) already carries plugin attribution — that attribution becomes
  registration: framework-side cleanup (`clear_plugin_registrations`) merges
  into the same dispose.
- Optional `async def dispose()` plugin hook. Fixed order: ① plugin
  `dispose()` (framework state still intact) → ② `ctx.effect` table, LIFO →
  ③ framework-side registry cleanup. Failures are logged and continue.
- MCP payload dispose is **not** specified here: the daemon's self-managed
  lifecycle (reap/ghosts) is deleted by #1212 Steps 3–5; this spec only
  defines the ready judgment that #1212's gateway routing consumes.

### Install hooks

The `install` phase allows exactly two steps — declaration validation + landing
(manifest/schema/dependency/scan checks, then copy + registry row). Install
hooks execute no runtime behavior — same discipline as #1212's "not served →
not started". Today's install paths already do this; the spec makes it the
stated rule.

## Context model (S5)

`Context = {ava_home, db_role, cluster_scope, machine, enabled_set}` — five
dimensions that today are scattered (D13), one concept.

- **Instance binding**: each extension instance resolves its context at
  creation and receives it explicitly; the loader refuses cross-context
  references (the worktree guard generalizes: no extension materializes
  content from a checkout that is not this home's).
- **Capability → resource gate**: a package declaring `db: none` is refused at
  any DB-handle injection point; `db: rw` is an explicit declaration. Test
  loaders default to `db: none` + `cluster_scope: throwaway` — dangerous
  resources are unreachable by default, the 2026-08-12 17:57 lesson made
  structural instead of a testing-hygiene habit.
- **Test context rules** (specifying existing discipline): env block before
  project imports; derived keys to a tmp-home `.env`; `shared/test_db_guard.py`
  fail-closed validation remains the single rule source.
- **Multi-tenant**: today one user, context rooted at `AVA_HOME`; the tenant
  dimension extends with #1212 Step 5. This spec only requires context to be
  an object packages declare against and the framework injects.

## Borrow / not-borrow

### Borrowed from Cordis

| Mechanism | Where it lands |
|---|---|
| Declarative service deps (`inject`) | `dependencies.plugins` + pre-load resolution; missing → `failed`, never silent |
| `ctx.effect(cleanup)` / `collect` | `PluginContext` effect table — LIFO disposal + automatic registration of existing `register_*` calls. **The core borrow**: resource reclamation must not depend on the author remembering |
| dispose-as-rollback | disable/update semantics (dispose → load), at the process-restart boundary |
| declarative schema validation | `config.schema` + pre-install validation, one declaration consumed everywhere |
| fork isolation | borrowed by the **context model** (S5): instance-bound context, test context default-deny — the spirit, not the mechanism (no process-internal fork trees) |

### Not borrowed from Cordis

1. **Everything-is-a-plugin / Context service graph** — the three payload
   kinds are text / cross-process protocol / in-process code; a service graph
   idles skills, rewrites MCP (conflicting with #1212), and throws away the
   seven hardened injection surfaces.
2. **Event bus (`ctx.on`/`emit`)** — measured plugin-to-plugin communication
   demand: zero. Premature complexity; add it above the dependency declarations
   if it ever appears.
3. **`reusable` multi-instance / fork trees** — Ava plugins are per-machine
   singletons; per-agent parameterization already exists (config overlay /
   birth config).
4. **In-process HMR** — the reload boundary is `self.restart` (user ruling
   2026-08-13 13:18); hot reload is complexity for a boundary that does not
   exist.

### Borrowed from VS Code

| Mechanism | Where it lands |
|---|---|
| One `package.json` + `contributes` | `ava-plugin.json` + the seven contribution surfaces |
| `engines.vscode` | `engines.ava` |
| `extensionDependencies` | `dependencies.plugins` — resolved + refused, **not** auto-installed (auto-installing third-party code violates the install-scan gate) |
| `activationEvents` | `lifecycle.activation` — the slot is declared (`immediate` only); event-driven activation is not implemented (six plugins, cheap imports — no benefit) |
| `capabilities.untrustedWorkspaces` | `dependencies.hostCapabilities` + the context model — declared capability, unreachable by default |

### Not borrowed from VS Code

1. **Marketplace / publishing channel** — Ava stays git-URL + local registry
   (user ruling 2026-08-12: generic MCP servers are user-installed and
   user-maintained; a store is infra a single-user deployment does not need).
2. **UI contribution surfaces (menus/views/themes)** — Ava plugins have no UI
   surface today; the key that grows when that changes is designed in
   [`future/frontend-plugin-contributions.md`](../future/frontend-plugin-contributions.md)
   (`contributions.ui`: declarative inspect sections / nav pages / theme token
   packs — the declarative *shape* is borrowed, runtime component composition
   still is not).
3. **Auto-generated activation event sets** — activation surface = agent
   process start; no workspace/language/command triggers.

## Resolved decisions (authoritative)

- **Manifest filename**: `ava-plugin.json` (2026-08-13). Not
  `.claude-plugin/plugin.json` — the adapter keeps translating CC packages.
- **`pythonPackages` enforcement**: hard (2026-08-13). Unbounded declaration =
  validator error (lint fail-fast); pyproject range outside the declared range
  = install/upgrade refused.
- **`failed` recovery**: `failed` → dispose; next load retries; manual
  re-enable; no auto-resurrection loop (2026-08-13).
- **Reload boundary**: agent process `self.restart`; no in-process hot reload
  (2026-08-13 13:18). Dispose = ordered cleanup at restart/uninstall/disable.
- **Schedule**: S0–S2 land before the open-source release (2026-08-13 13:15);
  S3–S5 after it, synchronized with #1212 Steps 3–5.

## Appendix — registry schema v2 evolution draft (S0 deliverable)

Proposed new `InstalledPackage` fields; **draft only, none implemented** — they
land with the lifecycle work (S3), which is also when the registry `version`
bumps 1 → 2.

| Field | Type | Semantics | Legacy-row derivation |
|---|---|---|---|
| `version` | `str \| None` | manifest-declared package version | `None` = no manifest |
| `installed_version` | `str \| None` | the version the last install/upgrade landed | `None` |
| `engines` | `dict[str, str] \| None` | declared host ranges (e.g. `{"ava": ">=0.1.38"}`) | `None` |
| `manifest_hash` | `str \| None` | sha256 of the manifest file as landed | `None` |
| `state` | `"installed" \| "enabled" \| "ready" \| "failed" \| "disposed" \| None` | lifecycle state (S3) | derive on read: `enabled` when the enable bit is set, else `installed` |
| `last_error` / `last_error_at` | `str \| None` / `str \| None` | activation failure + timestamp (S3) | `None` |

Reading stays backward compatible: every new field is optional, old rows read
as "no manifest, installed" — no migration, no silent re-trust. The manifest
file and the registry row stay bound by `manifest_hash`: the registry is *what
is installed*, the manifest is *what it claims to be* — a third party that
lies in the manifest is caught by the scan gate + trust tier, unchanged.
