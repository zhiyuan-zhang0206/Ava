# Skills and MCP as namespace context, not dispatch entries

## Context

Ava needs to grow into a general agent via two capability-extension channels:
authored skills (reusable task workflows) and an MCP consumer (connecting to
external MCP servers). The constraint is the single-tool philosophy: the agent
has exactly one `execute_code` tool, and all capability is exposed through the
`ava.*` Python namespace. Any extension mechanism must not reintroduce
per-capability tool dispatch or per-capability JSON schema maintenance. The
sandbox model is also fixed: one subprocess per turn, spawned and killed each
turn, with `ava` imported inside it.

## Decision

Both channels expose through `ava.*` and stay code-as-action.

**Skills** are discoverable prompt context, not a dispatch path. A skill is a
`SKILL.md` (frontmatter `name` + `description`, free-form body) plus arbitrary
attached files in its directory. The agent `list()`s skills, reads the capped
descriptions to pick a relevant one, `read()`s its full body as prompt context,
then writes Python following the flow itself. There is no "invoke" verb.

**MCP** is consumed through a generic call surface: `servers()`, `tools(server)`,
`call(server, tool, **args)`, `call_raw(...)`, `help()`. Tools are reached by
string name through `call`, not by dynamic attribute. Connections are persistent
within a subprocess (lazy-connect on first use, reuse the session for that
subprocess's lifetime) but never cross subprocess boundaries — each turn's
subprocess reconnects once and its MCP children die with it. Only stdio transport;
config is a Claude-Code-compatible `mcp.json` subset.

## Alternatives rejected

- **Skill as dispatch entry / new tool.** Each skill becoming a callable tool
  means a new schema to maintain per skill and breaks the single-tool model.
  Markdown context lets the agent read, plan, and write the code itself,
  preserving code-as-action. Rejected.

- **MCP via dynamic attribute (`ava.mcp.fs.read_file(...)`).** Looks ergonomic
  but requires lazy module magic and produces vague errors — a server- or
  tool-name typo surfaces as `AttributeError` rather than a precise class. The
  generic `call()` gives clean error layering (`MCPServerNotFound` vs
  `MCPToolNotFound` vs `MCPCallError`). Ergonomic wrapping, if wanted, can be
  layered later via the plugin wrap hook. Rejected for MVP.

- **Spawn an MCP connection per call.** Each spawn costs ~250ms; a typical turn
  lists tools plus a few calls, so spawning each time multiplies the latency the
  agent feels. Persistent-within-subprocess pays ~250ms only on first call and
  ~10ms after, for ~50 extra lines (a background asyncio loop bridged to the sync
  API). Rejected.

- **Reuse MCP connections across subprocesses.** Would leak connection state out
  of the per-turn subprocess and break the sandbox isolation that one-spawn-one-
  death guarantees. The cost avoided (one ~250ms reconnect per turn) is
  acceptable. Rejected.

- **Multi-source skill packs / namespacing.** The filesystem already enforces
  uniqueness — one `skills/<name>/` directory per name. Supporting layered packs
  (authored + third-party) adds machinery for no present need. Rejected as YAGNI.

## Consequences

- Adding a capability is authoring a `SKILL.md` or pointing `mcp.json` at a
  server — no SDK code, no tool schema, no system-prompt edit. Both surfaces
  appear automatically in `ava.help()`.
- The agent owns the decision of which skill applies and how to act on it; skills
  are advisory context, never a forced control path.
- Errors are explicitly layered into named classes (`SkillNotFound` /
  `SkillFormatError`; `MCPServerNotFound` / `MCPConnectError` / `MCPToolNotFound`
  / `MCPCallError`) so failures point at their cause rather than degrading into
  generic Python exceptions.
- MCP latency is amortized within a turn but reset across turns — a deliberate
  trade of one reconnect per turn for sandbox isolation.
- Deferred surface accepted: HTTP/SSE transport, dynamic-attribute ergonomics,
  and multi-source skill packs are all left for later, to be added only when a
  real need appears.
