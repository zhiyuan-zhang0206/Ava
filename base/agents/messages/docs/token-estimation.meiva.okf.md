---
type: doc
title: Text token estimation
description: Text token estimation.
tags: [contracts]
---

# Text token estimation

The estimator apportions a provider total among messages, parts and prompt
sections. It charges 0.95 per CJK character and 0.36 per other non-whitespace
character. Per-message overhead is 2 for AI and 15 for other messages. Text-only
weights omit overhead so parts can be compared. Unicode whitespace is free;
`str.split` performs that scan in native code. Multimodal blocks contribute
text/thinking content, never image base64. AI parts distinguish reasoning,
output and execute-code tool calls.

Historical calibration: the original coefficients were least-squares fitted on
3,255 exact messages of preview agent 9 on 2026-10-08. The source note reported
6.6% held-out median error versus 34% for chars/4, on one largely Chinese agent
and one provider. This is calibration provenance, not a current accuracy claim;
refit on new data instead of hand-tuning coefficients.
