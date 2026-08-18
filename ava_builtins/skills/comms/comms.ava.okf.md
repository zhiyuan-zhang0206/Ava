---
type: doc
title: Communication & User Interaction Skills
description: Agent skills for user-facing input/output — launch web pages to display content or collect structured replies, read SMS verification codes on macOS, and a full Gmail client. All built into the core repo (origin=repo).
tags:
- extensions
- agent-instruction
---

# Communication & User Interaction Skills

## What is it
A group of skills for the agent to interact with **users/external communication channels**: launch web pages to display content or collect structured replies, read SMS verification codes on macOS, and a full Gmail client for sending/receiving. All built into the core repo (origin=repo).

> **Directory note**: this node is a **capability grouping**, not a code directory — the skill code lives in its own top-level directory (`ava_builtins/skills/{ava-ui,sms,gmail}/`), while the OKF docs for the three skills sit together here under the functional group. The full directory layout of `ava_builtins/skills/` is the single source of truth for where a skill's code lives.

| Skill | Purpose | Detail |
|-------|---------|--------|
| ava-ui | Launch web pages to display content / collect replies (markdown+LaTeX, choice/confirm/form/compare panels) | [[ava_builtins/skills/comms/ava-ui.ava.okf.md]] |
| sms | Read SMS/iMessage verification codes via macOS Messages.app (on-demand script, not a daemon) | [[ava_builtins/skills/comms/sms.ava.okf.md]] |
| gmail | Full Gmail client (read/search/send/reply/forward/draft + newsletter, pure stdlib IMAP/SMTP) | [[ava_builtins/skills/comms/gmail.ava.okf.md]] |

## Key Dependencies
- [[ava/skills.ava.okf.md|Skill System]] — skill mechanism and core-vs-instance origin axis
- [[ava/ui.ava.okf.md|ava.ui]] — the ava-ui skill rests on the `ava.ui.serve/notify/show` SDK
