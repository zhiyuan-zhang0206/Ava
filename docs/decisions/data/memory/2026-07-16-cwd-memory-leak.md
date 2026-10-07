# CWD MEMORY.md Cross-Agent Leak

## Discovery

2026-07-16: a newly spawned agent loaded another agent's personal memory
(MEMORY.md) instead of its own. Investigation found that many agent
workspaces were contaminated with the same foreign content.

## Root Cause

`agent/graph/_memory_inject.py:per_agent_memory_note()` had a cwd fallback:

```python
cwd_mem = Path.cwd() / "MEMORY.md"
if cwd_mem.is_file() and cwd_mem.resolve() != path.resolve():
    cwd_text = cwd_mem.read_text(encoding="utf-8").strip()
    ws_text = path.read_text(encoding="utf-8").strip() if ws_exists else ""
    if cwd_text and not ws_text:
        path.write_text(cwd_text, encoding="utf-8")
```

Problem chain:

1. An agent left its personal memory in the shared checkout's `MEMORY.md`
   (cwd-relative writes from the `ava_code` plugin path resolution).
2. Every agent process's OS-level cwd is the checkout directory (inherited
   when the gateway starts agents).
3. When each new agent runs for the first time, `per_agent_memory_note()`
   finds `Path.cwd()/MEMORY.md` exists with content, the workspace's own
   MEMORY.md is empty (first run), and syncs the foreign content into the
   new agent's workspace.

## Fix

Remove the cwd fallback. Per-agent MEMORY.md now only reads from
`workspace_dir(agent_id)/MEMORY.md`.
