# MCP tool calls stay untyped (no Pydantic validation); a display-only signature is the only addition

## Context

MCP tools are called as `ava.mcps.<server>.<tool>(...)`. Each tool's
`input_schema` is stored as a JSON Schema dict in `ToolInfo` (`ava/mcps.py`),
rendered into the callable's `__doc__`, and reachable as raw JSON. The call path
(`_call_text` / `_call_raw`) forwards `**kwargs` to the server with no
client-side validation.

A proposal surfaced to generate Pydantic models from each tool's JSON Schema and
use them for type checking, schema validation (wrong type / missing required
field), IDE autocomplete, and richer introspection — citing that today there is
none of those.

The load-bearing fact for the decision: the caller of `ava.mcps.chrome.navigate(...)`
is the **model**, writing Python inside `execute_code(code=...)` and reading the
`help()` text. It has no LSP, no IDE, no autocomplete. `help(ava.mcps.chrome)`
renders each tool through `_format_signature` (`ava/__init__.py`); because the
callable is `def call(**kwargs: Any)`, the agent sees `def navigate(**kwargs: Any):`
followed by the full JSON Schema in the docstring. The only thing actually lost
is that the signature line reads `(**kwargs)` instead of the real parameter
names.

## Decision

**Do not add Pydantic models, and do not add pre-call validation.** The four
"gaps" are deliberate non-features, each the reverse of a core principle
(fail-fast, single `execute_code` tool, removable SDK surface). The fail-fast
loop already works: the MCP server rejects a bad call with a precise error, and
`_call_text` re-raises the server's message as `MCPCallError` back to the model.

The one real gap — the `(**kwargs)` signature line — is closed with a
**display-only** `inspect.Signature` synthesized from the JSON Schema
(`_schema_to_signature` in `ava/mcps.py`), attached as `call.__signature__`. It
does **no** validation and is never consulted on the call path; the body stays
`(**kwargs)` → `_call_text`. Required vs optional is conveyed only by the
presence or absence of a default. `help()` now renders
`def navigate(*, url: str, timeout: int = None) -> str:`.

```python
_JSON_PRIMITIVE = {"string": str, "integer": int, "number": float,
                   "boolean": bool, "array": list, "object": dict}

def _schema_to_signature(schema: dict[str, Any]) -> inspect.Signature | None:
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return None
    # Non-identifier (hyphen, leading digit) or Python-keyword (`from`) param
    # names are rejected/mis-rendered by inspect.Parameter -> fall back to
    # (**kwargs); the JSON schema in the docstring still lists every parameter.
    if not all(isinstance(n, str) and n.isidentifier() and not keyword.iskeyword(n)
               for n in props):
        return None
    required = set(schema.get("required") or [])
    params = []
    for name, pschema in props.items():
        ptype = pschema.get("type") if isinstance(pschema, dict) else None
        ann = _JSON_PRIMITIVE.get(ptype, Any) if isinstance(ptype, str) else Any
        default = inspect.Parameter.empty if name in required else None
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY,
                                         annotation=ann, default=default))
    return inspect.Signature(params, return_annotation=str)
```

## Alternatives rejected

### Pydantic models / pre-call kwarg validation

1. **Contradicts fail-fast.** Pre-call validation is the textbook "patch a
   mistake the model might make." The server is the sole schema authority and
   already returns a precise error the model can read and fix. A client gate
   duplicates that, creating two sources of truth for "what is valid."

2. **JSON Schema → Pydantic is lossy, so the copy is worse than the original.**
   `oneOf`/`anyOf`/`allOf`, conditional schemas, `format`, `additionalProperties`,
   `$ref` are all translation-loss points. A generated model that ends up
   stricter than the server falsely rejects calls the server would accept —
   strictly worse than no model; one that ends up looser adds nothing. The copy
   cannot be more correct than the authority.

3. **The IDE-autocomplete / `.pyi`-stub motivations have no audience.** No human
   hand-writes these call sites; the model generates them at runtime against
   servers discovered at runtime (`load_mcp_config` merges built-in `mcps/`,
   plugin `.mcp.json`, machine `mcp.json`). The tool surface is per-machine and
   dynamic, so there is nothing to enumerate into static stubs at build time.

4. **It grows a layer meant to get thinner.** "Output parsing, SDK surface" are
   listed as *removable* layers; the single-`execute_code`-tool decision
   explicitly rejected per-capability schema dispatch. A model-per-tool
   reintroduces exactly that machinery.

5. **"Better introspection" is worse introspection.** JSON Schema is the format
   the model is most fluent in (the MCP standard, every tool-use API) and is
   already in the docstring. A generated model's repr drops descriptions, enum
   values, and constraints unless every keyword is hand-mapped — a lossy
   re-rendering of the richer raw schema.

### The implementation questions, answered

| Question | Answer |
|---|---|
| Generate Pydantic models from JSON Schema at runtime? | Possible (`datamodel-code-generator`; or `pydantic.create_model` + a hand-written translator — Pydantic v2 has no first-class `from_json_schema`). Rejected per the above. |
| Dynamic (at access) vs pre-generation? | Servers are discovered at runtime, so only dynamic is coherent; pre-generation cannot enumerate them. That impossibility is itself evidence the feature does not fit. |
| Manual vs auto-generation? | Manual per-tool models drift the instant a server changes its schema (violates "code is the documentation; no shims"). Auto-generation collapses to the runtime lossy translation. Both lose. |
| Per-tool runtime overhead? | `create_model` builds a class + validators per tool, rebuilt per `execute_code` subprocess — pure overhead for zero gain, since the server validates anyway. |
| API surface change (validate kwargs before call)? | "Validate before call" *is* the fail-fast violation. Only a display-only `__signature__` is acceptable. |
| `.pyi` stubs for IDE support? | No audience (agent has no LSP) and no enumerable surface (runtime-dynamic servers). Skipped. |
| Interaction with the 24h disk cache? | The cache stores `input_schema` as JSON. A Pydantic class is not JSON-serializable, so it cannot be cached; it would be rebuilt from the cached schema in every process — more per-process cost, same persisted bytes. The cache already does the right thing. |

### Doing nothing at all (not even the signature)

Considered: the JSON Schema block already conveys every parameter, so a strong
model needs nothing more. Rejected only narrowly — the synthesized signature is
a small, self-contained, no-enforcement, trivially removable readability win
(`del call.__signature__` reverts it), and turning `(**kwargs)` into named
parameters on the signature line is a real if marginal improvement to the
`help()` view. It carries none of the costs above precisely because it validates
nothing.

## Consequences

- The MCP call path remains `(**kwargs)` → server; the server stays the schema
  authority; fail-fast is intact. A wrong type or missing field still fails at
  the server, surfaced as `MCPCallError`, not intercepted client-side.
- `help()` shows real parameter names for tools whose schema has plain-identifier
  property names. Tools with non-identifier or keyword parameter names, or no
  `properties`, fall back to `(**kwargs)` — the docstring's JSON schema still
  lists every parameter, so nothing is hidden.
- The added surface is one helper plus two lines, with no new dependency and no
  effect on the cache, the daemon, or the wire protocol. It is fully removable
  if a future model makes even the signature line redundant.
