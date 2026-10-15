---
type: doc
title: Durable service selection
description: Explicit roster intent survives retries, new plugins and transient exclusions.
tags: [deployment, services]
---

# Durable service selection

`service_selection.py` owns desired service intent independently of a start
attempt. Omitted selection keeps the prior intent. An `only` selection continues
to exclude newly installed plugins; `--all-services` explicitly resets it to the
complete roster. Internal temporary omissions never rewrite the saved selection.

`base/deploy/lifecycle/tests/test_service_selection.py` proves retained selection,
explicit reset, transient exclusion and refusal before a write. The operator
entry is described in [[cli/docs/start_identity.ava.okf.md]].
