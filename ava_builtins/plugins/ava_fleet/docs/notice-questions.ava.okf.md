---
type: doc
title: Numbered Notice Questions
description: Immutable single-select, multi-select and open notice questions with consistent numbering across Web, CLI, IM and agent replies.
tags:
- notify
- user-communication
---

# Numbered questions and answers

One notice carries one question. An agent with another question waits for the
current answer before posting it; posting still supersedes the old open notice.
The shared contract lives in `base.api_contracts.notice_question`.

```python
ava.ui.notify(
    "Which surfaces should change?",
    content="Choose the scope for this iteration.",
    require_response=True,
    question={
        "type": "multi_select",
        "options": [
            {"id": "web", "label": "Web notification center"},
            {"id": "inspector", "label": "Agent inspector"},
        ],
        "allow_other": True,
    },
    idempotency_key="notice-scope-decision",
)
```

Options are numbered **1, 2, ... in their declared order**, not sorted by ID,
label, recommendation or selection. Other, when allowed, gets the final number.
Numbers remain identical in Web, inspector, CLI, IM, history and agent inbounds.
A reference to an option inside Other is text, never an implicit selection.

Web submits `answer` with `option_ids`, optional `other_text` and `comment`; text
questions submit `answer.text`. Single-select accepts exactly one option or Other;
multi-select accepts at least one option or Other and can combine them. No default
selection or click-to-submit. Other requires nonblank text when selected.
The gateway validates against the stored question and stores both the canonical
answer and its readable reply in the existing resolution/inbound transaction.

CLI and IM keep their text reply entry: use option numbers separated by commas on
the first line (`2,3`), then Other text on subsequent lines if Other was selected,
or an optional note otherwise. Open questions accept plain multiline text.
Invalid, duplicate, unknown or empty selections fail without resolving the notice.
Closing a question without answering delivers a dismissal, never authorization.
