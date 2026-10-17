---
type: doc
title: "meiva_code — Coding Convention Plugin"
description: "`meiva_code` is Meiva's built-in coding convention plugin. It injects two system prompt sections and an after_exec hook, enabling the agent to automatically follow project coding conventions (AGENTS.md/worktree/PR workflow) and automatically perceive project context files (delivered in-memory inside the exec turn — see below)."
tags:
- extensions
- plugin
- agent-extension
---

# meiva_code — Coding Convention Plugin

## What it is

`meiva_code` is Meiva's built-in coding convention plugin: it contributes two system-prompt sections (`coding_tools_section` and `engineering_workflow_section`), `meiva.cwd`, and an `after_exec` hook that auto-injects `AGENTS.md`/`CLAUDE.md` context notes inside the exec turn.

## Declared hooks

### Context file auto-injection (in-memory, inside the exec turn)

```python
class _InjectCwdNotesAfterExecHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: object, _config: object, /
    ) -> dict | None: ...

# declared in contribute(): after_exec=(_InjectCwdNotesAfterExecHook(),)
```

- Wraps `meiva.files.read`: when the agent reads a file, traverses from the resolved path upward to git root or `$HOME` (whichever is farther), collecting `AGENTS.md` / `CLAUDE.md` along the way
- Each context file first undergoes `scan_content` for **prompt-injection security scanning** (a flagged file appends a `SecurityFindingEntry` to the exec turn's `meiva.state_update`; the after_exec hook `agent/hooks/security.py` delivers it as a SECURITY note), then the wrap appends a CONTEXT **system note** directly to the exec's messages delta via the declared base `messages` channel (`PluginStateHandle.update`) — **no side-channel file** (user ruling 2026-08-11)
- The `exec` node (`agent/graph/exec/node.py`) merges the plugin's messages delta into its own messages delta **after** the exec-result ToolMessage (security notes follow from the after_exec hook, also after it): the Anthropic-compat wire contract requires an AIMessage's `tool_use` to be immediately followed by its `tool_result` (DeepSeek anthropic endpoint 400s on interleaved text — verified 2026-08-11), and the dangling-tool_use repair hook would otherwise synthesize a fake `[interrupted]` result every turn
- Oversized context files (over `settings.sandbox.exec_output_max_chars`) are injected truncated head+tail with the full text archived to the workspace `.exec_output/` ring — the same overflow logic as exec output (`truncate_both_ends`); the archive path is reported in the note
- Deduplication: `injected_paths` + `injected_hashes` (content hash, prevents same content different path from being re-injected); after compact, reset lazily by `compact.version` (built-in `CompactState` sub-state) to resurface
- Primary path priority: system prompt directs agent to first `meiva.files.read("AGENTS.md")`; when going via that primary path, content is already in the return value, just marked, not re-injected
- Host-side cwd and project-skills note delivery: [[meiva_builtins/plugins/meiva_code/docs/runtime-notes.meiva.okf.md]].

### Plugin state declaration

```python
def state_handle() -> PluginStateHandle[MeivaCodeState]:
    return PluginStateHandle(MeivaCodeState, "meiva_code")  # a view over the turn's state slot, built per use
# declared in contribute(): state=(MeivaCodeState,)
# the SDK surface (`plugin.py`) reads and writes it through `read_state()` / `update_state(delta)`
```

`MeivaCodeState` lives in `_state.py` and the system-prompt sections in `_prompt_sections.py`: both import `meiva`, which `agent_runtime.py` (hooks and `contribute()`) does not.

- `MeivaCodeState` besides `cwd: str` (default = agent workspace or `$HOME`) declares the base `messages` channel (exact `BaseAgentState` annotation — the in-memory delivery channel for context notes) and holds context injection dedup state (`injected_paths` / `injected_hashes` / `last_seen_compact`) and note injection state (`cwd_note` / `project_skills_note` / `project_skills_seen_compact`)
- `cwd` is read/written via `meiva.cwd.get() / set()` and is purely logical LangGraph state: `set()` never mutates the parent or disposable child's OS cwd
- SDK wraps (`meiva.files.read/edit/write/append/delete/glob`, `meiva.shell.run`, `meiva.understand`) resolve relative paths using cwd (for `meiva.understand` the wrap walks the batch and resolves each target's `path`)

### SDK namespace declaration

```python
PluginContributions(sdk_namespaces=(SdkNamespace("cwd", _code_namespace, expand=True),), ...)
```

- `meiva.cwd.get()` → returns the current logical working directory
- `meiva.cwd.set(path)` → changes the logical working directory (relative path resolved against the current logical value, `~/...` expanded). MeivaCode's `files` / `shell` / `understand` wrappers read it explicitly; bare `open`, `Path.cwd`, imports, and user subprocesses retain their Python process cwd. After set, writes `cwd_note` (and when git repo has `.claude/skills` / `.agents/skills` / `.meiva/skills` project-local skills, writes `project_skills_note`), which are injected as system notes by the above after_exec hook
- Cwd initialization and repair: [[meiva_builtins/plugins/meiva_code/docs/runtime-notes.meiva.okf.md]].

## Key dependencies

- [[system-prompt.meiva.okf.md]] — system prompt construction
- [[agent/hooks/docs/hooks.meiva.okf.md]] — hook system
- [[agent/graph/exec/docs/exec.meiva.okf.md]] — fault-isolated code execution (where after_exec hook runs)

## Configuration

- Project-local skills: discovered when `meiva.cwd.set` at git root's `.claude/skills` (Claude Code compatible), `.agents/skills` (open Agent Skills standard) and `.meiva/skills` (Meiva repo local — last, so it takes priority) (`base/packages/skills/project_roots.py:project_skill_roots`)
- Direct child-side state construction uses `MeivaCodeState.cwd`'s `default_factory=default_cwd` (local SDK identity → `workspace_dir(agent_id)`, no identity → `$HOME`). This schema default is not authoritative for the host: the plugin's `after_init` hook initializes the missing graph channel from the explicit invocation context

## Notes

- This plugin is crucial for coding agents — disabling it means the agent won't know about AGENTS.md, worktree/PR workflow, or `meiva.cwd`
- The plugin owns cwd initialization and repair; thereafter the agent can change its saved logical cwd through `meiva.cwd.set`
