# Memory

The shared pool's index — every agent sees this file at the start of a session
and after a compact. Two sections, and they hold different kinds of thing.

`## Setup` is inline: the few facts every agent must have before doing anything,
short enough to carry in every context window.

`## Pointers` is one line per note, and only a line — a note's body lives in its
own file. Each pointer carries a title, a path, and a description; the
description is what a reader judges relevance by without opening the note:

    - [Title](path.md) — one-line description of what the note holds

Keep this file under 16000 characters; the commit hook rejects it otherwise,
which is the signal to move detail into a pointed-to note.

## Setup (keep current, do not drop)

- To change Ava itself, go through a pull request, CI, merge, then an update.
  Never edit the running install's source in place.
- Dev work happens in a separate checkout, not the running install.
- (add this machine's paths, roles, and quirks here as you learn them)

## Pointers

- (none yet — add one line per note as the pool grows)
