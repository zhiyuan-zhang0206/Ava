# Command vs UI event — the composer taxonomy

> Status: governing convention (2026-06-14). Most of it is already how the code
> works; the forward part is the `/`-namespace rule that keeps it from drifting.

Two kinds of thing can act on an agent from the outside. Keep them distinct.

| | **Command** | **UI event** |
|---|---|---|
| What it is | a prompt the model reads and reasons about | a control operation on the agent's process / state |
| Defining test | **inserts a message** into the conversation | does **not** insert a message |
| Examples | `/plan`, `/recap`, `/compact` | stop, terminate, restart, fork, the compact button |
| Where it lives | a `commands/<name>.md` template, expanded by `ava/_commands.py:expand_command` | a dedicated endpoint / button (e.g. `POST /api/agents/{id}/compact`) |
| Source-neutral | yes — a peer agent can send it as a message | no — a peer acts via an SDK call (`ava.agents.terminate(peer)`), not a message |

The single line between them: **does it put a message in front of the model?**
If yes, it is a Command; the model decides what to do with it. If no, it is a UI
event; it manipulates the agent without asking the model.

## The `/`-namespace rule

**A `/`-typed token is always a Command — it always expands to a message. UI
events are never `/`-typed; they are buttons / controls.**

This is the forward-looking half: do not make UI events (`/restart`,
`/terminate`, ...) typeable. Two reasons:

1. **Source-neutrality breaks.** Commands are sendable agent-to-agent. A peer
   sending `/terminate` *as a message* is meaningless — terminating a peer is an
   SDK call, not a message. UI events have no coherent message form.
2. **It would fork the parser.** Routing some `/`-tokens to `expand_command` and
   others to an endpoint is exactly the kind of dispatch shim the small core
   avoids. One rule — `/` means "expand to a message" — needs no dispatch.

## Several commands in one message

A send may invoke more than one command (`/plan the migration /recap`). **One
send is one message**: the whole chain expands inside that single inbound, in
the order typed, each command keeping the free text that followed it. Splitting
the send into one message per command is the thing not to do — separate
messages are claimed as separate turns, so the model answers each command
without seeing the rest, and neither ordering nor atomicity survives.

**Every command is the same kind of thing to the expander.** It never
classifies commands, never treats a combination as special, and never refuses
one; a chain is exactly the concatenation of what each command expands to
alone, and the expander adds no framing of its own around it. Whether two
prompts make sense together is a question about the prompts, and the agent
reading them answers it. `/compact` tells the agent to replace its own context,
so an instruction chained after it lapses — that is the prompt's meaning
playing out, not a case for the mechanism to predict, intercept, or warn about.
Adding a "these two don't go together" rule would move prompt semantics into
the transport, where it cannot be corrected by editing a prompt.

The mutual exclusion the claim node *does* enforce (a `compact_request` batched
with a `restart` loses) lives at the inbound **kind** layer — process
operations racing each other, not prompt text. It is not a precedent for
inspecting command text.

## Collisions dissolve by construction

Because the two live in different planes (the `/` namespace vs buttons), a word
can appear in both without ambiguity. `/compact` (the agent-driven, housekeeping
command) and the compact button (the framework's automatic compaction) coexist:
typing `/compact` is unambiguously the Command; the button is a separate
control. There is no name to resolve — the surface disambiguates.

If typed ergonomics for control ops are ever wanted, that is a **separate
palette** (its own prefix or menu), kept out of the message-command `/`
namespace — not a special case inside it.
