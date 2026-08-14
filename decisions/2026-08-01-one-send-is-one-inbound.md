# One send is one inbound — and every command expands alike

## Context

The Composer accepts `/`-commands, and users type more than one at a time
(`/plan the migration /recap`). PR #1000 shipped support for this by splitting
the input in the browser (`parseCommandSegments`) and calling
`POST /api/agents/{id}/messages` once per segment.

Each inbound row is claimed as its own turn. So the agent read `/plan …`,
answered it, and only afterwards discovered `/recap …` as an unrelated request.
The composite instruction never existed anywhere the model could see it: no
ordering guarantee (the claim batches whatever happens to be pending), no
atomicity (a failure mid-chain delivers half the intent), and no way for the
model to treat "plan this, then recap" as one thing.

## Decision

**One send is one message.** The Composer sends the raw text unsplit;
`ava._commands.expand_command` splits the chain and expands every command
inside that single inbound, in the order typed, each keeping the free text that
followed it. `parseCommandSegments` is deleted.

**Every command expands alike.** A command is a named prompt template and
nothing else. The expander does not classify commands, does not treat any
combination as special, and does not refuse anything. A chain is exactly the
concatenation of what each command expands to on its own, with no framing the
mechanism invented.

Segment boundaries are resolved against the command catalog: a later `/token`
opens a new command only if it names a registered one. `/recap check
/path/to/file` is therefore one command with a path in its argument.

## Alternatives rejected

**Keep the client-side split (PR #1000).** The defect is not in the splitting
regex, it is in the dispatch: N inbounds are N turns, and no amount of
client-side care makes them one instruction. The split also cannot be correct
where it lives — the browser does not have the command bodies, so it could only
guess at boundaries by punctuation, which is how a file path in free text
became a second "command".

**Classify commands and refuse bad combinations.** An intermediate version of
this change gave a command an `exclusive: true` frontmatter flag, set it on
`/compact` (whose body tells the agent to replace its own context), and refused
any chain containing it — 400 at the send endpoint plus an error from the
expander. Rejected. `/compact` is a prompt like every other command; that
instructions after it lapse is what its prompt *means*, and the agent reading it
works that out. Encoding the consequence in the transport buys nothing the
prompt does not already say, and costs a classification axis on every command, a
validation call on the send path, and a rule that can only be changed by editing
core code rather than a `.md` file. Keeping the mechanism uniform is the
small-core reading; predicting prompt semantics is not the mechanism's job.

**Put a preamble in front of a chain** ("carry these out in order, as one
request"). Rejected for the same reason: each expansion is already self-labelled
`Command /name:`, so concatenation is unambiguous without it, and the preamble
was the mechanism telling the agent how to interpret a combination.

**Reuse the claim node's compact-vs-restart exclusion as precedent.** That
mutual exclusion is at the inbound **kind** layer — two process operations
racing for the same turn. Command text is not a process operation, and the
analogy does not carry.

## Consequences

- Expansion is the receiver's job, uniformly — the same chain works from the web
  Composer and from `ava.agents.send_message(peer, "/a … /b …")`, because both
  reach the same claim node.
- No send path inspects command text. The gateway stores it; the claim node
  expands it. Adding a command needs no change anywhere but its `.md` file.
- A chain that reads oddly reaches the agent and the agent deals with it. That is
  the accepted trade: the failure mode is a confused turn, recoverable by the
  user rewording, rather than a refusal the user cannot override.
- The `/`-autocomplete still only serves the first command on the line (#1172).
  This decision does not address it; the fix belongs in the composer's caret
  handling, not in a pre-split array.

**Forward note:** that autocomplete gap is now closed — `parseSlash` is scoped to
the token the caret is in, so every command in a chain gets the dropdown. The
decision above is unchanged: the composer still sends one raw string and the
claim node still does all expansion.
