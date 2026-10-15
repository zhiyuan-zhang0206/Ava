---
type: doc
title: Pull Request Command Timeouts
description: Test boundaries for timeout handling in the pull request commands.
tags: [ci, testing]
---

# Pull Request Command Timeouts

```ava-contract
id = "queue-fixture-token"
kind = "invariant"
statement = "The freshness-timeout test passes a literal fixture token only through its injected FakeUrlOpener, with no real credential or network request. The exact S106 suppression identifies this intentional test input at the call site; moving the same literal behind a constant would only hide it from the security check. The test still verifies both expected queue requests."
code = ["scripts/ci/pull_requests/tests/test_command_timeouts.py:test_freshness_timeout_still_submits_the_green_pr"]
tests = ["scripts/ci/pull_requests/tests/test_command_timeouts.py::test_freshness_timeout_still_submits_the_green_pr"]
```
