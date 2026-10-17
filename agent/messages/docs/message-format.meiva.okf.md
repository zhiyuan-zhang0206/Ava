---
type: doc
title: Message Format
description: Message formats exchanged between agent, LLM, users, and other agents. `agent/messages/` defines inbound message construction; `_chat_inbound.py`
tags: []
---

# Message Format

## What it is

Message formats exchanged between agent, LLM, users, and other agents. `agent/messages/` defines construction of various messages; `_chat_inbound.py` assembles `kind='chat'` inbound rows into HumanMessage (including inline multimodal images).

## Message Types

### Inbound Messages (`inbound_message`)
- `inbound_message(*, content, source, inbound_id, body_start, created_at=, image_urls=, message_id=)` — envelope wrapper (the header is `base/agents/messages/envelope.py:inbound_head`; `meiva_inbound_body_start` records its length so readers cut the content out by offset and never parse the header; the sender is `meiva_source`). Inbounds written before the field existed show whole, header included, in the hierarchy catalog
- `source` is the original source string (`"system"` / `"agent:N"` / `"user"`), `meiva_inbound_id` records the source row id so history readers can correlate a message with its queue row. `inbound_id=None` (no key written) is for the one inbound with no queue row, the impersonation consent request, which is stamped with both times like every other injected message
- `content` plain text or multimodal block list
- Two times in metadata: `meiva_created_at` keeps its meaning (an inbound's is its ARRIVAL time, the source row's `created_at`; other messages' is when they were produced). `meiva_picked_up_at` is when an injected message (inbound, note, attachment) was put into the LLM context, i.e. when the model reads it; the builders stamp it. AIMessage and tool output are produced inside the context, so their `meiva_created_at` already is the read time and they carry no `meiva_picked_up_at`. Time-ordered readers (timeline, history tree, history dump) use `message_read_time()` (`meiva_picked_up_at`, else `meiva_created_at`); delivery tracking uses `inbound_messages.created_at`. The body envelope header (`wrap_inbound`) keeps the arrival time so the agent knows when a message came in.

### Inbound timestamp ownership

`base.agents.messages.envelope.EnvelopeReadInputs` carries the producer's live
clock factory and timestamp-policy reader. `inbound_head` and `wrap_inbound`
require those inputs; construction reads neither. User and agent headers read
the display flag first and obtain a clock only when timestamps are enabled.
System and asserted-caller headers return before either reader. Arrival-time
formatting and source validation keep their existing order.

### NoteTag Enum
- Marks the source and nature of the message
- Used by agent to distinguish user messages vs agent messages vs system notifications
- canonical definition + `meiva_msg_type` discriminator (`MeivaMsgType`) + typed reading `read_meiva_kwargs()` all in `base/agents/messages/kwargs.py` (see [[messages.meiva.okf.md]])

### System Messages (`system_note_message`)
- Builds system notification messages such as heartbeats, watcher wake-up calls
- `security_note_message(*, source, triggers, created_at=)` is the one writer of the SECURITY note for a prompt-injection scan finding (source + matched triggers, never the content); the claim node (behind a flagged inbound) and the exec node (child-drained findings) both use it

### Exec Output (`exec_output_message`)
- Wraps sandbox execution results from `execute_code`
- Contains merged stdout/stderr output
- `created_at` is a required argument: every writer (exec node, repair hook, syntax-fix plugin) stamps `meiva_created_at`, which is also the read time

### Attachments (`attach_message`)
- Appends one HumanMessage at the completed-turn boundary for files registered with `meiva.self.attach`
- Content blocks are interleaved per file: a leading notice text block, then each file's caption line directly before its model-native media block (`[text(notice), text(line1), media1, ...]`); `meiva_msg_type="attach"` distinguishes it without image URL metadata, and the timeline pairs every image with its own caption line structurally

### Chat Inbound Assembly (`_chat_inbound.py`)
- Assembles `kind='chat'` inbound rows into `(HumanMessage, finding)`: plain text via envelope wrapper as string message; multimodal inbound places text as first block, then uploads referenced images as native base64 blocks inline to the model. `finding` is the injection-scan result for the row's text (None when clean); the claim node owns delivering it as a SECURITY note
- Split out from `claim/node.py`, focused on multimodal image inline path

## Key Dependencies

- [[graph.meiva.okf.md]] — message passing between LangGraph nodes
- [[system-prompt.meiva.okf.md]] — system prompt + message history = LLM input
- [[context-window.meiva.okf.md]] — message history is the main consumer of the context window

## Entry Points
- `agent/messages/__init__.py:inbound_message(*, content, source, inbound_id, body_start, created_at=, image_urls=, message_id=)` — envelope-wrapped inbound message
- `agent/messages/__init__.py:system_note_message(...)` — system notification (with `NoteTag`)
- `agent/messages/__init__.py:security_note_message(...)` — SECURITY note for one injection-scan finding
- `agent/messages/__init__.py:exec_output_message(...)` — execution output; one shape for every ending. The structured record is `meiva_exec_status` (`ExecStatus`: completed / failed / timed_out / cancelled / halted / not_run / unknown), `meiva_exec_started_at` (the end is `meiva_created_at`), `meiva_exec_ms` and `meiva_exec_body_start` (where the body begins after the header); readers use these fields and never parse the header text, which is for the model. Results written before these fields existed carry no offset; the hierarchy catalog shows them whole, header included, instead of parsing the header
- `agent/messages/__init__.py:attach_message(...)` — attached media for the next turn
- `agent/graph/claim/_chat_inbound.py` — chat inbound → (HumanMessage, scan finding) assembly (multimodal inline)
