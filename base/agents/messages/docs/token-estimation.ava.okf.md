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

```ava-contract
id = "unicode-token-weight"
kind = "invariant"
statement = "Native whitespace scanning preserves CJK and other character weights for all Unicode characters and large text runs."
code = ["base/agents/messages/token_estimate.py:<module>"]
tests = ["base/agents/messages/tests/test_token_estimate.py::test_character_weights_cover_unicode_and_keep_whitespace_free", "base/agents/messages/tests/test_token_estimate.py::test_large_code_and_multilingual_runs_preserve_weight"]
```
