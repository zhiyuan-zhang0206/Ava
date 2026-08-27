---
type: doc
title: Message Format
description: Message formats exchanged between agent, LLM, users, and other agents. `messages.py` defines inbound message construction; `_chat_inbound.py`
tags: []
---

# Message Format

## What it is

Message formats exchanged between agent, LLM, users, and other agents. `messages.py` defines construction of various messages; `_chat_inbound.py` assembles `kind='chat'` inbound rows into HumanMessage (including inline multimodal images).

## Message Types

### Inbound Messages (`inbound_message`)
- `inbound_message(*, content, source, inbound_id, created_at=, image_urls=)` — envelope wrapper (product of `shared/envelope.py:wrap_inbound`)
- `source` is the original source string (`"system"` / `"agent:N"` / `"user"`), `ava_inbound_id` records the source row id for startup reconcile
- `content` plain text or multimodal block list

### NoteTag Enum
- Marks the source and nature of the message
- Used by agent to distinguish user messages vs agent messages vs system notifications
- canonical definition + `ava_msg_type` discriminator (`AvaMsgType`) + typed reading `read_ava_kwargs()` all in `shared/message_kwargs.py` (see [[messages.ava.okf.md]])

### System Messages (`system_note_message`)
- Builds system notification messages such as heartbeats, watcher wake-up calls

### Exec Output (`exec_output_message`)
- Wraps sandbox execution results from `execute_code`
- Contains merged stdout/stderr output

### Attachments (`attach_message`)
- Appends one HumanMessage at the completed-turn boundary for files registered with `ava.self.attach`
- Starts with the packer's text caption and follows with model-native media blocks; `ava_msg_type="attach"` distinguishes it without image URL metadata

### Chat Inbound Assembly (`_chat_inbound.py`)
- Assembles `kind='chat'` inbound rows into `HumanMessage`: plain text via envelope wrapper as string message; multimodal inbound places text as first block, then uploads referenced images as native base64 blocks inline to the model
- Split out from `_claim.py`, focused on multimodal image inline path

## Key Dependencies

- [[graph.ava.okf.md]] — message passing between LangGraph nodes
- [[system-prompt.ava.okf.md]] — system prompt + message history = LLM input
- [[context-window.ava.okf.md]] — message history is the main consumer of the context window

## Entry Points
- `agent/messages.py:inbound_message(*, content, source, inbound_id, created_at=, image_urls=)` — envelope-wrapped inbound message
- `agent/messages.py:system_note_message(...)` — system notification (with `NoteTag`)
- `agent/messages.py:exec_output_message(...)` — execution output
- `agent/messages.py:attach_message(...)` — attached media for the next turn
- `agent/graph/_chat_inbound.py` — chat inbound → HumanMessage assembly (multimodal inline)
