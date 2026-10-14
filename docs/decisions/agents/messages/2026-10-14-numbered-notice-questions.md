# One numbered question per notice

## Scope

A user supervising multiple agents needs to answer one decision quickly while
retaining the ability to express a different answer and refer to numbered choices.
A notice remains one question, one submission and one agent wake. Existing queue
priority, blocking, one-open-notice-per-agent and superseding semantics remain.

## Prior art

Native HTML radio groups, checkboxes and textareas provide the interaction model.
Pydantic discriminated models validate the shared wire contract; the existing
notice transaction and operation receipts already own delivery and retry identity.

## Simplest sufficient design

Extend notify with one optional question rather than another questionnaire API.
Persist question/answer JSONB on the notice; existing notices remain free-text.
Use the declared array order as stable 1-based numbering. Other is the last
number, with text required when selected. A structured answer records option IDs,
Other text or open text, and an optional note. The server creates the readable
reply from the validated answer and original numbered options.

## Context

The June free-text decision accepted A/B/C conventions to avoid parsing choices
out of arbitrary Markdown. The user now requests explicit single-/multi-select,
Other and open questions, with numbers usable as references inside Other.

## Decision

The producer supplies typed options; the frontend never infers them from prose.
Freeze question, title and context at creation so numbers and their meaning cannot
change while a user answers. Replace the notice for semantic changes. Allow
priority/blocking edits. Web submits structured answers; CLI/IM adapt a numbered
first line and following Other/note text through the same validation contract.
No selection is preset and clicking a choice never submits. Submit explicitly;
dismissal remains a distinct system event. Store the answer and its inbound in
the existing atomic transaction, preserving retries and legacy receipt shapes.

## Consequences

The original numbering survives UI, history and the self-describing agent inbound.
Single-select enforces one option or Other; multi-select can add Other to options.
References in Other do not silently select those options. Typed questions cannot
be edited in place, and text-only clients must follow the displayed number grammar.
The UI keeps existing FYI/legacy controls while replacing structured-question
controls with readable numbered rows and explicit answer submission.

## Alternatives considered

- Parse options from Markdown: ambiguous numbering and unreliable single/multiple
  selection semantics. Explicit producer-owned data prevents that failure.
- Add a separate question primitive/table: duplicates queue, lifecycle and receipt
  owners for an operation that already belongs to notify.
- A batch of questions in one notice: rejected by the user; one notice is one question.
- Reorder choices by recommendation or selected state: breaks numeric references.
- Allow semantic edits in place: lets an answer authorize context the user did not see.
