---
type: doc
title: Unavailable model capabilities
description: "Frontend behavior when model capability metadata is unavailable: nullable supports_vision, gating, accepted-receipt limits, and browser-test scope."
tags: [frontend]
---

# Unavailable model capabilities

The required nullable `supports_vision` field distinguishes unavailable model
metadata from a known text-only model. A successful roster or selected-detail
read replaces the previous capability value, including true-to-null changes;
lifecycle SSE events remain invalidation hints and cannot resurrect old metadata.
Agent status, history selection and bounded directory pagination retain their
existing meanings. See [[base/agents/observation/docs/observation.ava.okf.md]].

The composer explains null capability and enables native image attachment only
for true. Ordinary file delivery remains a separate upload/receipt operation.
Text sending and exact-work stop remain accessible; their accepted receipts do
not claim model execution or work completion. A failed models query hides cached
model choices and disables spawning, with an unavailable label in both expanded
and collapsed controls. Ordinary Control configuration remains editable.

The isolated browser test `tests/e2e/flow/test_provider_failure.py` starts the
real gateway with a provider module that raises, reads retained checkpoint
history, posts text and requests a stop through the real page. It verifies a
pending inbound and accepted cancellation while the recorded work remains
running; it does not run a model or assert a worker exited.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-data-flow/frontend-data-flow.ava.okf.md|Frontend Data Flow]] — the node this availability behavior belongs to; the field's owner is [[base/agents/observation/docs/observation.ava.okf.md|observation]].
