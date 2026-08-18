---
type: doc
title: ava.files — File System
description: Persistent file operation interface for agents. Relative paths resolve to the agent's workspace, `~/...` resolves to the user home.
tags: []
---

# ava.files — File System

## What it is

Persistent file operation interface for agents. Relative paths resolve to the agent's private workspace directory (each agent independent). `~/...` resolves to the user home. (The `ava_code` plugin overlays mutable `ava.cwd` tracking on top; core baseline always uses the workspace, never silently falls back to `$HOME`.)

## Core API

- `read(path, start=None, end=None, *, limit=None, with_line_numbers=False) → str` — read a file, supports 1-indexed line ranges. `limit` and `end` are mutually exclusive. Read content passes through injection scanning (clean content returned as-is).
- `write(path, content)` — overwrite, auto-creates parent directories. When writing to memory pool notes, if injection pattern is hit, marks `injection-risk: flagged` in its frontmatter.
- `append(path, content)` — append, creates if not exists.
- `edit(path, old, new, *, replace_all=False)` — exact replacement. `old` defaults to matching once only; `replace_all=True` replaces all occurrences. On no match, error returns a diff hint of the closest match.
- `delete(path)` — delete file (does not delete directories).
- `glob(pattern='*') → list[Path]` — glob matching (supports `**` recursion).

## Constraints
- Relative paths resolve to the agent's workspace (each agent independent)
- `edit()` does not allow fuzzy matching — `old` must be exact
- Does not operate on directories (`delete` only deletes files)

## Key Dependencies
- [[self.ava.okf.md]] — workspace belongs to the agent itself, `ava.self` carries identity
