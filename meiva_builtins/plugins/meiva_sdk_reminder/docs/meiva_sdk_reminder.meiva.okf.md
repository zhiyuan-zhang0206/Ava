---
type: doc
title: meiva_sdk_reminder — SDK Reminder Plugin
description: '`meiva_sdk_reminder` surfaces smoother SDK primitives, explains cross-cell NameErrors caused by fresh interpreters, and points agent replies at the delivery primitive.'
tags:
- extensions
- plugin
- agent-extension
---

# meiva_sdk_reminder — SDK Reminder Plugin

## What is it

`meiva_sdk_reminder` reminds the agent to use smoother SDK primitives when it uses native Python equivalents, explains cross-cell NameErrors caused by fresh interpreters, and points agent replies at the delivery primitive.

## Registered Hooks

### Post-code-execution detection (after_exec hook)

```python
class _SdkReminderAfterExecHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: Runtime[MeivaContext], _config: RunnableConfig, /
    ) -> dict | None: ...  # return state update dict (not string)

sdk_reminder_after_exec = _SdkReminderAfterExecHook()
# declared in contribute(): after_exec=(sdk_reminder_after_exec,)
```

Each call in the latest assistant message is matched to its ToolMessage by ID, including batches followed by notes or media. Code is read via the shared `first_tool_call_code` (`agent/graph/tool_calls.py`,
a strongly-typed extractor on the langchain `ToolCall` TypedDict). Detects four categories of native Python usage and injects reminders:

| Category | Detection pattern | Suggested SDK primitive |
|----------|-------------------|-------------------------|
| **Shell** | `subprocess.run/os.system` | `meiva.shell.run(cmd)` |
| **Wait** | `time.sleep(n)` loop polling | `meiva.watcher` (`at`/`cron`/`launch`) |
| **File** | `open()`/pathlib read-write/shutil content operations (listing/managing calls like `glob.glob`/`os.listdir`/`os.remove` do not trigger; string/comment literals are masked) | `meiva.files.read/write/edit/delete` |
| **HTTP** | `requests/httpx/urllib` requests | `meiva.web.fetch([(url, prompt)])` / `meiva.web.search([query])` |

The four categories share `sdk_code_reminder_cadence`: `once_per_compaction` tracks each in the `reminded` set and re-arms after compact, while `every_time` emits after every matching cell. The reminder is injected as a `system_note_message` — the agent reads it as framework aside, not mistaking it for code output.

### Assumed-persistence NameErrors (after_exec hook)

When the execution output contains a `NameError: name 'X' is not defined` traceback line (including Python's optional suggestion suffix), the hook searches earlier assistant `execute_code` calls for `X` as a whole identifier. If found, it explains that each cell runs in a fresh interpreter. Earlier calls in the same batch also count; the current cell is excluded, keywords and builtins are ignored, and each name is recorded as `nameerror:X` so it fires at most once per context window and re-arms after compact.

### Turn-taking reminder (before_llm hook)

When the agent receives a new message, inject a hint pointing to inter-agent communication primitives:

```python
class _SdkReminderAgentReplyHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: Runtime[MeivaContext], _config: RunnableConfig, /
    ) -> dict | None: ...
    # Remind the agent to use meiva.agents.send_message to reply to other agents
    # rather than '@agent in text' — text won't be delivered

sdk_reminder_agent_reply_before_llm = _SdkReminderAgentReplyHook()
# declared in contribute(): before_llm=(sdk_reminder_agent_reply_before_llm,)
```

## Key Dependencies

- [[agent/hooks/docs/hooks.meiva.okf.md]] — after_exec + before_llm hook
- [[agent/graph/exec/docs/exec.meiva.okf.md]] — code execution sandbox (after_exec hook runs here)
- [[sdk-surface.meiva.okf.md]] — the SDK surface being reminded about

## Configuration

- `MeivaSdkReminderState` in `_state.py`: persists the `reminded` set (which categories have been reminded)
- `contribute()` declares the state (`state=(MeivaSdkReminderState,)`), persisted across agent lifecycle
- `sdk_code_reminder_cadence`: shared cadence for the four code categories; defaults to `once_per_compaction`, with `every_time` available
- `sdk_nameerror_hint_enabled`: enables the precise assumed-persistence NameError hint by default; `false` disables it
- `agent_reply_reminder_cadence`: independent cadence for agent-reply hints with the same values and default

## Notes

- The default cadence avoids repeated reminders within a context window; agents that need stronger reinforcement can opt into `every_time`
- NameError hints remain once-per-name even when code-category cadence is `every_time`
- For the wait category, when encountering a watcher silence, it does not emit (mark-without-emit, see `plugin.py`)
- Mutual exclusion with auto-compact: agent_reply skips the inbound for turns where compact is predicted to trigger, and does **not mark** (leaving it for the next agent inbound to remind)
