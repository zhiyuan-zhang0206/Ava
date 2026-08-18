---
type: doc
title: ava-use-claude-code-and-codex skill — Drive external coding CLI
description: Treat claude (Anthropic) / codex (OpenAI) as "another agent", give it tasks, and let it plan+execute. Carries the judgment of when to outsource, to which one, and the file-driven collaboration pattern for supervising long tasks; the session primitives themselves are left to ava.shell, not repeated here.
tags:
- extensions
- agent-instruction
---

# ava-use-claude-code-and-codex skill — Drive external coding CLI

## What it is
`claude` and `codex` are both coding-agent CLIs that can be given tasks and let them plan+execute (`$AVA_HOME/skills/ava-use-claude-code-and-codex/`). Treat either as "another agent". This skill carries **judgment**: when to outsource, to which one, and the file-driven collaboration pattern for supervising long tasks. The session primitives themselves (`ava.shell`'s run / sessions, `ava.watcher`) are **not repeated here** — it explicitly points to those, not re-derives.

## Judgments carried
- **When to outsource**: single file read/write / grep / git / a single command → do it yourself; multi-step coding tasks that can be fully described (write+test+fix), expected >10 files with multiple rounds of trial and error → outsource.
- **Which one**: if you need a specific model's reasoning / long context, choose the tool backed by that model; for diff review, use `codex exec review` or `claude -p`. A machine may only have one installed — verify with `--version`/`--help` beforehand.
- **Persistent sessions are the default**: start in `ava.shell.sessions`, steer across rounds; headless one-shot (`claude -p` / `codex exec`) is rarely needed. **Flags and models drift with versions**, text is a snapshot not a contract.

## Key dependencies
- [[ava_builtins/skills/orchestration/orchestration.ava.okf.md|Workflow orchestration skill]] — belongs to functional group
- [[ava/shell/shell.ava.okf.md|ava.shell]] — `sessions` (new/send/send_keys/capture/kill) session primitive itself
- [[ava/watcher.ava.okf.md|ava.watcher]] — wait for it to produce results when supervising long tasks
