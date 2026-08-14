# decisions/

Curated, durable design decisions for this project — why X was chosen over Y,
what alternatives were rejected, and the trade-offs behind the choice. Not a
dated activity diary, not ops/bench logs, not implementation narration (those
live in the PR description + git log).

## Rules

- **One decision per file**, named `YYYY-MM-DD-<topic>.md` — flat, no date
  subdirectories, no numeric prefixes. The date prefix orders entries and
  records when the decision was made; the topic is kebab-case.
- Write an entry when a **directional design decision** gets made — not when
  a task merely finishes. Trivial bug fixes, formatting, pure translation,
  and pure ops actions don't get one.
- A decision is a **point-in-time snapshot**: never rewrite a past entry to
  match current reality. If a decision is later overturned, write a **new**
  entry and add a forward link at the end of the old one pointing to it — the
  old entry stays as a record of "what we believed then."
- Use [`_template.md`](_template.md) as the starting point for a new entry.
