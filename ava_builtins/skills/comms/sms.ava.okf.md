---
type: doc
title: SMS — SMS / Verification Code Reading
description: macOS Messages.app SMS reading skill — queries the local Messages database (`~/Library/Messages/chat.db`)
tags: []
---

# SMS — SMS / Verification Code Reading

## What is it
A macOS Messages.app SMS reading **skill** (`ava_builtins/skills/sms/`) — queries the local Messages database (`~/Library/Messages/chat.db`) to retrieve 2FA verification codes and recent SMS messages. It is a self-contained on-demand script, not a persistent daemon, and not on the service roster (`build_services()`) — when an agent needs a 2FA code, it invokes it via `ava.skills.sms`, following SKILL.md and running `.venv/bin/python ava_builtins/skills/sms/scripts/query.py`.

## Core Responsibilities
- **Verification code extraction**: `query_codes(phone_suffix, lookback_hours)` — extracts numeric verification codes from SMS using regex
- **Message query**: `query_messages(phone_suffix, lookback_hours)` — fetches recent SMS messages
- **Access pattern**: reads chat.db directly via sqlite3; when blocked by TCC, `sqlite3.OperationalError` is raised directly (no fallback — fail fast, resolves by granting Full Disk Access to the calling process)
- **CLI interface**: `.venv/bin/python ava_builtins/skills/sms/scripts/query.py --recent-codes --phone-suffix 1118`

## Key Dependencies
- [[tool-calls.ava.okf.md]] — agent invokes this capability via the `ava.skills.sms` skill

## Entry Points
- `ava_builtins/skills/sms/scripts/query.py:query_codes()` — extract verification codes
- `ava_builtins/skills/sms/scripts/query.py:main()` — CLI entry
- `ava_builtins/skills/sms/SKILL.md` — skill manifest + usage

## Notes
- macOS only (depends on `~/Library/Messages/chat.db`)
- On-demand skill script, not a persistent daemon
