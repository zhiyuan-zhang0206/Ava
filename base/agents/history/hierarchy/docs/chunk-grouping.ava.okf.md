---
type: doc
title: Chunk Grouping
description: What a chunk call asks and accepts — the instruction, the numbered catalog of layer-0 units, and the group envelope whose first / last numbers code checks and resolves.
tags:
- hierarchy
- understanding
- chunks
---

# Chunk Grouping

## Grouping inside the call (`chunk_generate.py`, `leaf_groups.py`)

English instruction after the prefix. It opens with a line setting it apart from the messages
before it (the prefix can end in a framework reminder that would otherwise read as part of the
task), then: divide the part listed in the catalog into consecutive groups, keeping consecutive
units about the same matter together, and summarize each (a node above the raw messages, much
shorter, not a handoff, in the conversation's language); the catalog is the complete ordered
list, referred to by number, not to be matched against the messages above, and the summaries
describe only the listed units; what a unit is; parenthesized lines are framework messages to join
to a neighbour. The **numbered catalog** has one unit per line: `[number] type: content`
(`build_catalog`, no time; `units.catalog_line`, whitespace collapsed). The type is code-made:
inbound the sender (`human message`, `agent N message`, `watcher N`), `agent text`, `work`.
Inbound and text show 100 characters of content; work shows `reasoning[..] | call[..] |
output[..]` (corner brackets), each 60 characters, an empty part left out, the call without import lines; a framework **note** shows a label by type (`units.note_label`: `(memory)`,
`(system note)`, `(compact summary)`, `(attachment)`, a note's tag otherwise). A chunk of only
notes is `skipped` without a call. Nothing else is prescribed. Every unit is in a group. The reply
names each group by the numbers of its first and last unit, like the upper levels:

    <group first="1" last="16">summary of units 1 to 16</group>
    <group first="17" last="30">summary of units 17 to 30</group>

Code looks the numbers up (`leaf_groups.resolve_groups`); no text is matched. Both ends are
written so the numbers can be checked: the groups must tile the catalog (first group at 1, each
starting right after the previous `last`, `last` not before `first`, the last group ending at the
catalog's last unit). A model that numbers its groups 1, 2, 3 instead of naming units cannot end
its last group at the catalog's end, so it is refused and not stored with every span shifted
(agent 9, job 149). Other collected problems: a malformed envelope (a `<group` / `<open` tag that
is not a complete element: a missing `</group>` would merge groups; there is no `<open>`), a
number not in the catalog, an empty summary. A group starting on a turn's tool calls whose text
unit comes right before starts at that unit, silently (spans stay unique). A refusal names the
offending group's first and last catalog lines (40 characters each) and says the numbers are unit
numbers, not group positions. Problems go back in the same conversation (prefix, instruction, its
own reply unchanged: cached) for corrected groups, up to `AVA_UNDERSTANDING_GROUP_CORRECTIONS`
(2); then `GenerateError`.

The call, queue and consumer around it: [[base/agents/history/hierarchy/docs/chunks.ava.okf.md|Chunk-triggered Understanding]].
