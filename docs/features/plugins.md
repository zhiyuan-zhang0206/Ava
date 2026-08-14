# Plugins

Plugins are Ava's runtime extension mechanism: custom behavior inserted into
the agent execution graph at five injection points — after init, before the
LLM call, before code execution, after code execution, and at context setup.

## Why it matters

- **Typed state** — a plugin registers a whole pydantic model that becomes a
  private channel in the agent's state graph: typed read/write handles,
  fail-fast on name conflicts, persisted and restored with the framework
  checkpoint.
- **No framework forks** — write a `plugin.py` in a directory and it loads at
  agent startup; the same API serves official plugins and third-party ones.
- **Surgical surface** — a plugin can contribute system-prompt sections,
  context notes, SDK namespace members, graph-edge hooks, and config classes;
  everything else stays untouched.

## How it works

```
plugin.py → loaded at agent startup → registers:
  graph-edge hooks (after_init / before_llm / before_exec / after_exec)
  plugin state (pydantic model → dynamic AgentState channels)
  SDK namespace members (ava.<plugin>.*), prompt sections, context notes
```

Built-in plugins: `ava_code` (cwd/AGENTS.md notes), `ava_sdk_reminder`
(SDK-primitive hints), `ava_silent_idle` (continue nudge), `ava_memory`
(memory recall), `ava_syntax_fix` (repair), `ava_fleet` (the human-supervision
surface: labels, notification queue, task registry).

## Design decisions

- [Plugin and hook layers](../../okf/plugins.ava.okf.md)
- [Plugin & hook layers decision](../../decisions/2026-05-13-plugin-and-hook-layers.md)
- [Plugin-core-boundary-wrapper-extension](../../decisions/2026-07-19-plugin-core-boundary-wrapper-extension.md)
