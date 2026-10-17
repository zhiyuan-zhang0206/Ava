---
type: doc
title: Trigger (frontend)
description: The frontend hook, state ownership and request lifecycle behind the suggestion candidates.
tags: [gateway, agents]
---

# Trigger (frontend)

`ui/web/src/lib/composer/use-reply-suggestion.ts` is the only writer of the candidates.
For the selected, visible agent it asks once per turn: the agent is idle, the last
conversation item is the agent's own message, and nothing new started for one second.
A new turn (busy again or a new last item), another agent, a hidden page, the user
setting `behavior.replySuggestions` (default on) turned off, or unmount aborts the request and
hides the suggestion. Opening an already-idle agent takes the same path. A settled turn is
remembered by agent and last item; an aborted one is not.

State ownership: the candidates are volatile, derived state, held in component state in
`HomeContent` (not TanStack Query, not Zustand) with the hook as its only writer;
`page.tsx` passes them to `Composer` as the `suggestions` prop. Because they are derived from the
busy signal, agent status, last item and tab visibility, it needs no clearing event. The
preference is read only after `useUserSettings` has loaded, so a stored "off" is never
raced by the default "on". `Composer` shows the text as its placeholder (so typing hides
them and clearing the input restores the selected candidate) and accepts on ArrowRight
in the empty input. Candidate selection is local to `Composer`, scoped to the response
array and agent. Accepting or editing does not consume candidates; only a successful
send consumes them, so clearing a failed send's draft restores the same selection.
A new response starts at the first candidate. Slash completion, IME composition,
modified arrows and arrows in nonempty drafts keep their ordinary behaviour.

## Relationship to Other Nodes

- [[gateway/agents/docs/reply-suggestion/reply-suggestion.meiva.okf.md|Reply Suggestion]] — the feature this trigger belongs to.
