---
type: doc
title: Impersonation checkpoint receipt test
description: Why the receipt-retry regression uses a real state graph.
---

# Impersonation checkpoint receipt test

The receipt-retry test uses a real compiled StateGraph and MemorySaver so the
additive reducer actually runs. A fake checkpoint dictionary would not establish
whether a failed receipt write causes the same delta to be applied twice. The
idle callback receives its unused state positionally; it does not adapt state
values or execute application work. LangGraph's overloads expose partially
unknown generic types and require the parameter name `state` in their callback
protocol. The narrow type directives keep this real runtime seam while the
test explicitly checks the resulting checkpoint value after both settlements.
