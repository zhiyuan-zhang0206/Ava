# Config: ownership decomposition of the Settings god object

## Context

`shared/config.py` hit the worst-three intersection: largest file in the repo
(3056 lines, one `Settings(BaseSettings)` with 162 flat fields), highest
import count (98 sites across 10 directories), high churn (14×/300 commits).
Ownership was only expressible as per-field `category` metadata policed by a
dedicated pre-commit hook. Every function depending on `settings` nominally
depends on the whole universe of configuration. `ops/schemas.py` (1517 lines,
93 pydantic models — the entire wire contract in one file) is the same
disease in the API plane, and it squats in the directory the ops layer needs.

## Decision

- **`shared/config/` package, split by owning domain**: `LmSettings`,
  `DataPlaneSettings`, `GatewaySettings`, `AgentSettings`, … as separate
  pydantic models aggregated into one `Settings` composite. All sub-models
  stay in the shared layer (the aggregate must live at the lowest layer;
  ownership is expressed by file, not directory).
- **Nested access**: `settings.lm.deepseek_api_key`. The prize is narrow
  dependency injection — `build_chat_model` takes `LmSettings`, not the
  world; blast radius becomes visible in signatures.
- **Env surface frozen**: existing env var names are user-facing contract,
  pinned via aliases/prefixes; the split is invisible to `.env`.
- **Structure replaces metadata**: the `ConfigFieldMeta`/category machinery
  and the "every field declares ownership" lint retire; the frontend config
  UI's registry is derived by walking sub-models.
- **Deployment identity moves to ops spec**: fields describing what this
  deployment *is* (cluster name, ports, machine host, secrets, data-plane
  addresses) belong to the ops Spec per the ops decision; behavioral knobs
  stay in settings. Without this cut, spec and config would each hold a copy
  of identity — the dual-expression disease again.
- **Wire schemas move to gateway**: `gateway/schemas/` package split by
  router family; types genuinely shared with the agent-runner RPC get one
  explicitly named contract module. Codegen (openapi → types-generated.ts)
  reads the FastAPI app and is unaffected.

## Alternatives rejected

- **Colocating settings classes in their owning subsystems' directories** —
  upward layering violation; agent/services import the aggregate from shared.
- **Keeping flat attribute access via delegation** — preserves call sites but
  hides ownership exactly where it should be visible.
- **Splitting by size instead of ownership** — cosmetic; the disease is
  world-dependency, not line count.
