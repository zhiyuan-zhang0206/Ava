---
type: doc
title: ava_sdk_reminder — SDK Reminder Plugin
description: '`ava_sdk_reminder` surfaces smoother SDK primitives, explains cross-cell NameErrors caused by fresh interpreters, and points agent replies at the delivery primitive.'
tags:
- extensions
- plugin
- agent-extension
---

# ava_sdk_reminder — SDK Reminder Plugin

## What is it

`ava_sdk_reminder` gently reminds the agent to use smoother SDK primitives when it uses native Python equivalents, explains cross-cell NameErrors caused by fresh interpreters, and points agent replies at the delivery primitive.

## Registered Hooks

### Post-code-execution detection (after_exec hook)

```python
class _SdkReminderAfterExecHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: Runtime[AvaContext], _config: RunnableConfig, /
    ) -> dict | None: ...  # return state update dict (not string)

sdk_reminder_after_exec = _SdkReminderAfterExecHook()
register_after_exec(sdk_reminder_after_exec)
```

Code is taken from `last_msg.tool_calls`, read via the shared `first_tool_call_code` (`agent/graph/_tool_calls.py`,
a strongly-typed extractor on the langchain `ToolCall` TypedDict). Detects four categories of native Python usage and injects reminders:

| Category | Detection pattern | Suggested SDK primitive |
|----------|-------------------|-------------------------|
| **Shell** | `subprocess.run/os.system` | `ava.shell.run(cmd)` |
| **Wait** | `time.sleep(n)` loop polling | `ava.watcher` (`at`/`cron`/`launch`) |
| **File** | `open()/shutil/os` file operations | `ava.files.read/write/edit/delete` |
| **HTTP** | `requests/httpx/urllib` requests | `ava.web.fetch([(url, prompt)])` / `ava.web.search([query])` |

The four categories share `sdk_code_reminder_cadence`: `once_per_compaction` tracks each in the `reminded` set and re-arms after compact, while `every_time` emits after every matching cell. The reminder is injected as a `system_note_message` — the agent reads it as framework aside, not mistaking it for code output.

### Assumed-persistence NameErrors (after_exec hook)

When the execution output contains a `NameError: name 'X' is not defined` traceback line (including Python's optional suggestion suffix), the hook searches earlier assistant `execute_code` calls for `X` as a whole identifier. If found, it explains that each cell runs in a fresh interpreter. The current cell is excluded, keywords and builtins are ignored, and each name is recorded as `nameerror:X` so it fires at most once per context window and re-arms after compact.

### Turn-taking reminder (before_llm hook)

When the agent receives a new message, inject a hint pointing to inter-agent communication primitives:

```python
class _SdkReminderAgentReplyHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: Runtime[AvaContext], _config: RunnableConfig, /
    ) -> dict | None: ...
    # Remind the agent to use ava.agents.send_message to reply to other agents
    # rather than '@agent in text' — text won't be delivered

sdk_reminder_agent_reply_before_llm = _SdkReminderAgentReplyHook()
register_before_llm(sdk_reminder_agent_reply_before_llm)
```

## Key Dependencies

- [[agent/hooks/hooks.ava.okf.md]] — after_exec + before_llm hook
- [[tool-exec.ava.okf.md]] — code execution sandbox (after_exec hook runs here)
- [[sdk-surface.ava.okf.md]] — the SDK surface being reminded about

## Configuration

- `AvaSdkReminderState` in `_state.py`: persists the `reminded` set (which categories have been reminded)
- `register_plugin_state` registers the state, persisted across agent lifecycle
- `sdk_code_reminder_cadence`: shared cadence for the four code categories; defaults to `once_per_compaction`, with `every_time` available
- `sdk_nameerror_hint_enabled`: enables the precise assumed-persistence NameError hint by default; `false` disables it
- `agent_reply_reminder_cadence`: independent cadence for agent-reply hints with the same values and default

## Notes

- The default cadence avoids repeated reminders within a context window; agents that need stronger reinforcement can opt into `every_time`
- NameError hints remain once-per-name even when code-category cadence is `every_time`
- For the wait category, when encountering a watcher silence, it does not emit (mark-without-emit, see `plugin.py`)
- Mutual exclusion with auto-compact: agent_reply skips the inbound for turns where compact is predicted to trigger, and does **not mark** (leaving it for the next agent inbound to remind)
