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
