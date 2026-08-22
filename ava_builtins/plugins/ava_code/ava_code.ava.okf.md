---
type: doc
title: "ava_code — Coding Convention Plugin"
description: "`ava_code` is Ava's built-in coding convention plugin. It injects two system prompt sections and an after_exec hook, enabling the agent to automatically follow project coding conventions (AGENTS.md/worktree/PR workflow) and automatically perceive project context files (delivered in-memory inside the exec turn — see below)."
tags:
- extensions
- plugin
- agent-extension
---

# ava_code — Coding Convention Plugin

## What it is

`ava_code` is Ava's built-in coding convention plugin. It injects two system prompt sections and an after_exec hook, enabling the agent to automatically follow project coding conventions (AGENTS.md/worktree/PR workflow) and automatically perceive project context files (delivered in-memory inside the exec turn — see below).

## Registered hooks

### System prompt injection (×2)

```python
@register_system_prompt_section
def _coding_tools_section() -> str:
    # cwd / ava.files / ava.shell stub descriptions + coding convention preamble
    # (fail fast, don't reinvent, worktree + PR workflow, AGENTS.md/CLAUDE.md role)
```

```python
@register_system_prompt_section
def _engineering_workflow_section() -> str:
    # Loose debug / bug-fix workflow advice (reproduce → root cause → fix)
    # Gated by "ava_code_workflow" ∈ settings.agent.system_prompt_extra
    # (env AVA_SYSTEM_PROMPT_EXTRA), **default empty = not injected by default**
```

These two are registered via `agent/graph/_system_prompt.py`'s `register_system_prompt_section`,
and appended at runtime to the agent's system prompt (the `_engineering_workflow_section` is off by default, not injected).

### Context file auto-injection (in-memory, inside the exec turn)

```python
class _InjectCwdNotesAfterExecHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: object, _config: object, /
    ) -> dict | None: ...

inject_cwd_notes_after_exec = _InjectCwdNotesAfterExecHook()
register_after_exec(inject_cwd_notes_after_exec)
```

- Wraps `ava.files.read`: when the agent reads a file, traverses from the resolved path upward to git root or `$HOME` (whichever is farther), collecting `AGENTS.md` / `CLAUDE.md` along the way
- Each context file first undergoes `scan_content` for **prompt-injection security scanning** (buffers a `SecurityFindingEntry` in-memory when flagged), then the wrap appends a CONTEXT **system note** directly to the exec's messages delta via the declared base `messages` channel (`PluginStateHandle.update`) — **no side-channel file** (user ruling 2026-08-11)
- The `exec` node (`agent/graph/_exec.py`) merges the plugin's messages delta and the drained security findings into its own messages delta, both **after** the exec-result ToolMessage: the Anthropic-compat wire contract requires an AIMessage's `tool_use` to be immediately followed by its `tool_result` (DeepSeek anthropic endpoint 400s on interleaved text — verified 2026-08-11), and the dangling-tool_use repair hook would otherwise synthesize a fake `[interrupted]` result every turn
- Oversized context files (over `settings.sandbox.exec_output_max_chars`) are injected truncated head+tail with the full text archived to the workspace `.exec_output/` ring — the same overflow logic as exec output (`truncate_both_ends`); the archive path is reported in the note
- Deduplication: `injected_paths` + `injected_hashes` (content hash, prevents same content different path from being re-injected); after compact, reset lazily by `compact.version` (built-in `CompactState` sub-state) to resurface
- Primary path priority: system prompt directs agent to first `ava.files.read("AGENTS.md")`; when going via that primary path, content is already in the return value, just marked, not re-injected
- The after_exec hook keeps only the cwd-change note and the project-skills note (summaries of plugin state, not content discovered during exec)

### Plugin state registration

```python
state_handle = register_plugin_state(AvaCodeState)
```

- `AvaCodeState` besides `cwd: str` (default = agent workspace or `$HOME`) declares the base `messages` channel (exact `BaseAgentState` annotation — the in-memory delivery channel for context notes) and holds context injection dedup state (`injected_paths` / `injected_hashes` / `last_seen_compact`) and note injection state (`cwd_note` / `project_skills_note` / `project_skills_seen_compact`)
- `cwd` is read/written via `ava.cwd.get() / set()` and is purely logical LangGraph state: `set()` never mutates the parent or disposable child's OS cwd
- SDK wraps (`ava.files.read/edit/write/append/delete/glob`, `ava.shell.run`, `ava.understand`) resolve relative paths using cwd (for `ava.understand` the wrap walks the batch and resolves each target's `path`)

### SDK namespace registration

```python
ava.register_namespace("cwd", _code_namespace)
ava.register_sdk_expand("cwd")
```

- `ava.cwd.get()` → returns the current logical working directory
- `ava.cwd.set(path)` → changes the logical working directory (relative path resolved against the current logical value, `~/...` expanded). AvaCode's `files` / `shell` / `understand` wrappers read it explicitly; bare `open`, `Path.cwd`, imports, and user subprocesses retain their Python process cwd. After set, writes `cwd_note` (and when git repo has `.claude/skills` / `.agents/skills` / `.ava/skills` project-local skills, writes `project_skills_note`), which are injected as system notes by the above after_exec hook
- `validate_cwd_after_init` → after checkpoint restore, validates the persisted logical cwd and repairs a stat failure or non-directory value to the agent workspace; it never calls `os.chdir`

## Key dependencies

- [[system-prompt.ava.okf.md]] — system prompt construction
- [[agent/hooks/hooks.ava.okf.md]] — hook system
- [[tool-exec.ava.okf.md]] — fault-isolated code execution (where after_exec hook runs)

## Configuration

- Project-local skills: discovered when `ava.cwd.set` at git root's `.claude/skills` (Claude Code compatible), `.agents/skills` (open Agent Skills standard) and `.ava/skills` (Ava repo local — last, so it takes priority) (`ava_builtins/plugins/ava_code/_walk.py:project_skill_roots`)
- `ava.cwd` initial value = plugin's own `AvaCodeState.cwd` Field `default_factory=_default_cwd` (has agent identity → `workspace_dir(agent_id)`, no identity → `$HOME`), **not** injected by agent-runner

## Notes

- This plugin is crucial for coding agents — disabling it means the agent won't know about AGENTS.md, worktree/PR workflow, or `ava.cwd`
- `ava.cwd`'s default comes from the plugin's `_default_cwd` (has identity = `workspace_dir(agent_id)`, no identity = `$HOME`); thereafter agent can `set` it on its own
