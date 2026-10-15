---
type: doc
title: RPC content bounds
description: Shared prompt and reply character constraints.
---

# RPC content bounds

`content.py` owns the shared prompt/reply string constraint. Its character cap
limits oversized input while allowing long handoffs and reports. It is not a
transport byte limit or a model context-token budget: Unicode encoding and
tokenization use different units.

```ava-contract
id = "content-guard"
kind = "invariant"
statement = "User content is stripped, must be nonempty, and is limited to one million characters; legitimate input longer than 64 KiB remains accepted."
code = ["ops/rpc_schemas/content.py:_MAX_CONTENT_CHARS"]
tests = ["tests/components/gateway/test_schemas_wire_format.py::test_user_message_in_strips_and_rejects_empty", "tests/components/gateway/test_schemas_wire_format.py::test_user_message_in_accepts_long_content_and_rejects_abuse_cap"]
```
