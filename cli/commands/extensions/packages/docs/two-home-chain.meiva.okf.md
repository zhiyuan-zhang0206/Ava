---
type: doc
title: Two-home extension chain test
description: What the sequential shared-database fixture establishes and excludes.
---

# Two-home extension chain test

`test_extension_two_home_chain.py` installs on one home and materializes from
the cluster registry on another home that never saw the source tree. Its
`as_machine` fixture switches identities sequentially. Both homes share one
PostgreSQL connection identity: the test does not cover independent credentials,
concurrent machines, or a network partition. Registry and materialization
assertions therefore establish this shared-database chain, not deployment-wide
authorization or concurrency guarantees.
