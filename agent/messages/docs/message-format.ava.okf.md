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
- `inbound_message(*, content, source, inbound_id, created_at=, image_urls=)` — envelope wrapper (product of `base/agents/messages/envelope.py:wrap_inbound`)
- `source` is the original source string (`"system"` / `"agent:N"` / `"user"`), `ava_inbound_id` records the source row id for startup reconcile
- `content` plain text or multimodal block list

### NoteTag Enum
- Marks the source and nature of the message
- Used by agent to distinguish user messages vs agent messages vs system notifications
- canonical definition + `ava_msg_type` discriminator (`AvaMsgType`) + typed reading `read_ava_kwargs()` all in `base/agents/messages/kwargs.py` (see [[messages.ava.okf.md]])

### System Messages (`system_note_message`)
- Builds system notification messages such as heartbeats, watcher wake-up calls
- `security_note_message(*, source, triggers, created_at=)` is the one writer of the SECURITY note for a prompt-injection scan finding (source + matched triggers, never the content); the claim node (behind a flagged inbound) and the exec node (child-drained findings) both use it

### Exec Output (`exec_output_message`)
- Wraps sandbox execution results from `execute_code`
- Contains merged stdout/stderr output

### Attachments (`attach_message`)
- Appends one HumanMessage at the completed-turn boundary for files registered with `ava.self.attach`
- Content blocks are interleaved per file: a leading notice text block, then each file's caption line directly before its model-native media block (`[text(notice), text(line1), media1, ...]`); `ava_msg_type="attach"` distinguishes it without image URL metadata, and the timeline pairs every image with its own caption line structurally

### Chat Inbound Assembly (`_chat_inbound.py`)
- Assembles `kind='chat'` inbound rows into `(HumanMessage, finding)`: plain text via envelope wrapper as string message; multimodal inbound places text as first block, then uploads referenced images as native base64 blocks inline to the model. `finding` is the injection-scan result for the row's text (None when clean); the claim node owns delivering it as a SECURITY note
- Split out from `claim/node.py`, focused on multimodal image inline path

## Key Dependencies

- [[graph.ava.okf.md]] — message passing between LangGraph nodes
- [[system-prompt.ava.okf.md]] — system prompt + message history = LLM input
- [[context-window.ava.okf.md]] — message history is the main consumer of the context window

## Entry Points
- `agent/messages/__init__.py:inbound_message(*, content, source, inbound_id, created_at=, image_urls=)` — envelope-wrapped inbound message
- `agent/messages/__init__.py:system_note_message(...)` — system notification (with `NoteTag`)
- `agent/messages/__init__.py:security_note_message(...)` — SECURITY note for one injection-scan finding
- `agent/messages/__init__.py:exec_output_message(...)` — execution output; ok / error / timeout / cancelled share one shape (outcome is in the text only; the `exec_failed` / `exec_timeout` / `exec_cancelled` log events are the only record)
- `agent/messages/__init__.py:attach_message(...)` — attached media for the next turn
- `agent/graph/claim/_chat_inbound.py` — chat inbound → (HumanMessage, scan finding) assembly (multimodal inline)
